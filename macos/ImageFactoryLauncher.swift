import AppKit
import Combine
import CoreFoundation
import Darwin
import Foundation
import SwiftUI

@main
struct ImageFactoryLauncherApp: App {
    @NSApplicationDelegateAdaptor(LauncherAppDelegate.self) private var appDelegate
    @StateObject private var launcher = LauncherModel()

    var body: some Scene {
        WindowGroup {
            LauncherWindow()
                .environmentObject(launcher)
                .frame(minWidth: 820, minHeight: 620)
        }
        .defaultSize(width: 940, height: 700)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

private final class LauncherAppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

@MainActor
final class LauncherModel: ObservableObject {
    enum Phase {
        case stopped
        case starting
        case running
        case stopping
        case failed
    }

    @Published private(set) var phase: Phase = .stopped
    @Published private(set) var message = "点击下面的按钮打开审核工作台。"
    @Published private(set) var projectRoot: URL?
    @Published private(set) var intakeMessage = "表单接单尚未启动。"
    @Published private(set) var intakeHasFailure = false

    private var process: Process?
    private var outputPipe: Pipe?
    private var outputBuffer = Data()
    private var intakeProcess: Process?
    private var intakeOutputPipe: Pipe?
    private var intakeOutputBuffer = Data()
    private var intakeOutputFailure: String?
    private var intakeRequestedStop = false
    private var entryURL: URL?
    private var requestedStop = false
    private var startupFailure: String?
    private var terminationObserver: NSObjectProtocol?

    init() {
        projectRoot = Self.detectProjectRoot()
        terminationObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.terminateOwnedProcesses()
            }
        }
        if projectRoot == nil {
            message = "没有找到项目文件夹。点“选择项目文件夹”选中 Image-Factory。"
        } else if !Self.hasActiveRun(projectRoot!) {
            message = "目前没有待审核任务。工作台仍可打开，新的任务会在建立后显示。"
        }
        if let projectRoot {
            updateIntakeAvailability(for: projectRoot)
        }
    }

    deinit {
        if let terminationObserver {
            NotificationCenter.default.removeObserver(terminationObserver)
        }
    }

    var isRunning: Bool { phase == .running }
    var isBusy: Bool { phase == .starting || phase == .stopping }

    var statusTitle: String {
        switch phase {
        case .stopped: return "服务未启动"
        case .starting: return "正在准备工作台"
        case .running: return "工作台已打开"
        case .stopping: return "正在停止"
        case .failed: return "启动没有成功"
        }
    }

    func chooseProjectFolder() {
        let panel = NSOpenPanel()
        panel.title = "选择 Image-Factory 项目文件夹"
        panel.message = "请选择同时包含 factory、config.local.json 和 .venv 的项目文件夹。"
        panel.prompt = "选择文件夹"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard Self.isProjectRoot(url) else {
            phase = .failed
            message = "这个文件夹不是完整的 Image-Factory 项目。请选项目最外层文件夹。"
            return
        }
        projectRoot = url.resolvingSymlinksInPath()
        phase = .stopped
        updateIntakeAvailability(for: projectRoot!)
        message = Self.hasActiveRun(projectRoot!)
            ? "项目已找到。点击“启动并打开审核工作台”。"
            : "项目已找到。可以打开工作台，目前暂无任务。"
    }

    func openWorkbench() {
        if phase == .running, let entryURL {
            NSWorkspace.shared.open(entryURL)
            message = "审核工作台已在浏览器中打开。"
            return
        }
        guard !isBusy else { return }
        guard let root = projectRoot ?? Self.detectProjectRoot() else {
            message = "请先选择 Image-Factory 项目文件夹。"
            phase = .failed
            return
        }
        guard Self.isProjectRoot(root) else {
            projectRoot = nil
            message = "项目文件夹不完整。请重新选择 Image-Factory 文件夹。"
            phase = .failed
            return
        }
        guard let port = Self.availablePort() else {
            message = "找不到可用的本机端口。请关闭其他图片工厂窗口后重试。"
            phase = .failed
            return
        }

        projectRoot = root
        phase = .starting
        message = "正在检查飞书登录状态并启动审核页面……"
        startupFailure = nil
        requestedStop = false
        outputBuffer.removeAll(keepingCapacity: true)
        entryURL = nil

        let child = Process()
        let pipe = Pipe()
        child.executableURL = root.appendingPathComponent(".venv/bin/python")
        child.arguments = [
            "-u", "-m", "factory",
            "--config", "config.local.json",
            "--state", "var/live",
            "human-ui",
            "--port", String(port)
        ]
        child.currentDirectoryURL = root
        child.standardOutput = pipe
        child.standardError = pipe

        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            DispatchQueue.main.async {
                self?.consumeOutput(data)
            }
        }
        child.terminationHandler = { [weak self] terminated in
            DispatchQueue.main.async {
                self?.childDidTerminate(terminated)
            }
        }

        process = child
        outputPipe = pipe
        do {
            try child.run()
            startIntakeLoopIfAuthorized(root: root)
        } catch {
            pipe.fileHandleForReading.readabilityHandler = nil
            process = nil
            outputPipe = nil
            phase = .failed
            message = "无法启动 Image-Factory：\(error.localizedDescription)"
        }
    }

    func stopService() {
        stopIntakeLoop()
        guard let process, process.isRunning else {
            self.process = nil
            entryURL = nil
            phase = .stopped
            message = "本机审核服务已停止。"
            return
        }
        requestedStop = true
        phase = .stopping
        message = "正在停止本机审核服务……"
        process.terminate()
    }

    private func consumeOutput(_ data: Data) {
        outputBuffer.append(data)
        if outputBuffer.count > 64_000 {
            outputBuffer = Data(outputBuffer.suffix(32_000))
        }
        while let newline = outputBuffer.firstIndex(of: 10) {
            let lineData = outputBuffer.prefix(upTo: newline)
            outputBuffer.removeSubrange(...newline)
            guard let line = String(data: lineData, encoding: .utf8) else { continue }
            consumeLine(line)
        }
    }

    private func consumeLine(_ line: String) {
        guard let data = line.data(using: .utf8),
              let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }

        if let entry = payload["entry"] as? String,
           let url = URL(string: entry),
           url.scheme == "http",
           url.host == "127.0.0.1",
           URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.contains(where: {
               $0.name == "access" && !($0.value ?? "").isEmpty
           }) == true {
            entryURL = url
            phase = .starting
            message = "服务已启动，正在确认页面可以访问……"
            waitUntilReady(url, remaining: 24)
            return
        }

        if let error = payload["error"] as? String {
            startupFailure = error
            phase = .failed
            if error.contains("Unknown run") {
                message = "这条任务已不存在。旧演示不会重新打开；请等待新任务建立。"
            } else if error.localizedCaseInsensitiveContains("飞书")
                || error.localizedCaseInsensitiveContains("lark") {
                message = "飞书在线验证没有通过。请先在飞书登录，再回到这里重试。"
            } else {
                message = String(error.prefix(240))
            }
        }
    }

    private func waitUntilReady(_ entry: URL, remaining: Int) {
        guard var components = URLComponents(url: entry, resolvingAgainstBaseURL: false) else {
            failStartup("入口地址无效，请重新启动 App。")
            return
        }
        components.path = "/api/v1/session"
        components.query = nil
        components.fragment = nil
        guard let probeURL = components.url else {
            failStartup("入口地址无效，请重新启动 App。")
            return
        }

        var request = URLRequest(url: probeURL)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self, self.phase == .starting else { return }
                if (response as? HTTPURLResponse)?.statusCode == 403 {
                    self.phase = .running
                    self.message = "审核工作台已在浏览器打开。使用时请保持这个 App 运行。"
                    NSWorkspace.shared.open(entry)
                    return
                }
                guard remaining > 0 else {
                    self.failStartup("服务没有及时响应。请检查项目文件夹和飞书登录状态后重试。")
                    return
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    self.waitUntilReady(entry, remaining: remaining - 1)
                }
            }
        }.resume()
    }

    private func failStartup(_ detail: String) {
        startupFailure = detail
        phase = .failed
        message = detail
        stopIntakeLoop()
        if let process, process.isRunning { process.terminate() }
    }

    private func childDidTerminate(_ child: Process) {
        guard process === child else { return }
        stopIntakeLoop()
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        process = nil
        outputPipe = nil
        entryURL = nil
        if requestedStop {
            phase = .stopped
            message = "本机审核服务已停止。"
        } else if let startupFailure {
            phase = .failed
            message = startupFailure
        } else if phase == .starting {
            phase = .failed
            message = "审核服务提前退出了。请重新启动；如果仍失败，请确认飞书已登录。"
        } else {
            phase = .stopped
            message = "本机审核服务已结束。再次点击即可启动。"
        }
        requestedStop = false
    }

    private func terminateOwnedProcesses() {
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        if let process, process.isRunning { process.terminate() }
        if intakeProcess?.isRunning == true {
            intakeRequestedStop = true
            intakeProcess?.terminate()
        }
    }

    private func updateIntakeAvailability(for root: URL) {
        intakeHasFailure = false
        if Self.hasAuthorizedIntakeConfiguration(root) {
            intakeMessage = "已配置明确且商品范围匹配的受信授权；启动工作台时会打开表单接单。"
        } else {
            intakeMessage = "表单接单未启用：本机缺少完整、商品绑定的 approved grant；审核工作台仍可打开。"
        }
    }

    private func startIntakeLoopIfAuthorized(root: URL) {
        guard intakeProcess?.isRunning != true else { return }
        guard Self.hasAuthorizedIntakeConfiguration(root) else {
            updateIntakeAvailability(for: root)
            return
        }

        intakeRequestedStop = false
        intakeOutputBuffer.removeAll(keepingCapacity: true)
        intakeOutputFailure = nil

        let child = Process()
        let pipe = Pipe()
        child.executableURL = root.appendingPathComponent(".venv/bin/python")
        child.arguments = [
            "-u", "-m", "factory",
            "--config", "config.local.json",
            "--state", "var/live",
            "intake-loop",
            "--interval", "5",
            "--max-cycles", "0"
        ]
        child.currentDirectoryURL = root
        child.standardOutput = pipe
        child.standardError = pipe

        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            DispatchQueue.main.async {
                self?.consumeIntakeOutput(data)
            }
        }
        child.terminationHandler = { [weak self] terminated in
            DispatchQueue.main.async {
                self?.intakeChildDidTerminate(terminated)
            }
        }

        intakeProcess = child
        intakeOutputPipe = pipe
        do {
            try child.run()
            intakeHasFailure = false
            intakeMessage = "表单接单循环已启动；它只排队任务，不生成图片。"
        } catch {
            pipe.fileHandleForReading.readabilityHandler = nil
            intakeProcess = nil
            intakeOutputPipe = nil
            intakeHasFailure = true
            intakeMessage = "表单接单循环启动失败：\(error.localizedDescription)。审核工作台仍可使用。"
        }
    }

    private func stopIntakeLoop() {
        guard let intakeProcess, intakeProcess.isRunning else { return }
        intakeRequestedStop = true
        intakeMessage = "正在停止表单接单循环……"
        intakeProcess.terminate()
    }

    private func consumeIntakeOutput(_ data: Data) {
        intakeOutputBuffer.append(data)
        if intakeOutputBuffer.count > 64_000 {
            intakeOutputBuffer = Data(intakeOutputBuffer.suffix(32_000))
        }
        while let newline = intakeOutputBuffer.firstIndex(of: 10) {
            let lineData = intakeOutputBuffer.prefix(upTo: newline)
            intakeOutputBuffer.removeSubrange(...newline)
            guard let line = String(data: lineData, encoding: .utf8),
                  let payload = try? JSONSerialization.jsonObject(
                    with: Data(line.utf8)
                  ) as? [String: Any],
                  let error = payload["error"] as? String else { continue }
            intakeOutputFailure = String(error.prefix(200))
            intakeHasFailure = true
            intakeMessage = "表单接单循环遇到错误：\(intakeOutputFailure!)。审核工作台仍可使用。"
        }
    }

    private func intakeChildDidTerminate(_ child: Process) {
        guard intakeProcess === child else { return }
        intakeOutputPipe?.fileHandleForReading.readabilityHandler = nil
        intakeProcess = nil
        intakeOutputPipe = nil
        if intakeRequestedStop {
            intakeMessage = "表单接单循环已停止。"
            intakeHasFailure = false
        } else {
            let detail = intakeOutputFailure ?? "退出码 \(child.terminationStatus)"
            intakeMessage = "表单接单循环已退出：\(detail)。审核工作台仍可使用。"
            intakeHasFailure = true
        }
        intakeRequestedStop = false
    }

    private static func hasAuthorizedIntakeConfiguration(_ root: URL) -> Bool {
        let configURL = root.appendingPathComponent("config.local.json")
        guard let data = try? Data(contentsOf: configURL),
              let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let grant = config["intake_grant"] as? [String: Any],
              let profile = config["intake_profile"] as? [String: Any],
              let approved = grant["approved"] as? NSNumber,
              CFGetTypeID(approved) == CFBooleanGetTypeID(), approved.boolValue,
              nonEmptyString(grant["authorization_id"]) != nil,
              nonEmptyString(grant["actor_id"]) != nil,
              let scope = grant["scope"] as? [String: Any],
              let profileNamespace = nonEmptyString(profile["namespace"]),
              let profileMode = nonEmptyString(profile["mode"]),
              profileNamespace == "V1-DEMO-KIDS", profileMode == "demo",
              nonEmptyString(profile["review_policy_version"]) == "demo-v2",
              let allowedProductIDs = profile["allowed_product_ids"] as? [String],
              !allowedProductIDs.isEmpty,
              allowedProductIDs.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }),
              let productRecordID = nonEmptyString(scope["product_record_id"]),
              allowedProductIDs.contains(productRecordID),
              nonEmptyString(scope["namespace"]) == profileNamespace,
              nonEmptyString(scope["mode"]) == profileMode,
              nonEmptyString(scope["category"]) == "kids_shoes",
              nonEmptyString(profile["workflow_id"]) != nil,
              nonEmptyString(profile["channel"]) != nil,
              nonEmptyString(profile["placement"]) != nil,
              let maxCalls = strictInteger(profile["max_calls"]), (1...6).contains(maxCalls),
              let grantLimit = strictInteger(grant["project_image_calls_limit"]),
              grantLimit >= maxCalls else { return false }
        return true
    }

    private static func nonEmptyString(_ value: Any?) -> String? {
        guard let string = value as? String else { return nil }
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func strictInteger(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.rounded(.towardZero) == number.doubleValue else { return nil }
        return number.intValue
    }

    private static func detectProjectRoot() -> URL? {
        let bundle = Bundle.main.bundleURL.resolvingSymlinksInPath()
        let candidates = [
            bundle.deletingLastPathComponent().deletingLastPathComponent(),
            bundle.deletingLastPathComponent()
        ]
        return candidates.first(where: isProjectRoot)
    }

    private static func isProjectRoot(_ root: URL) -> Bool {
        let fm = FileManager.default
        return fm.isExecutableFile(atPath: root.appendingPathComponent(".venv/bin/python").path)
            && fm.fileExists(atPath: root.appendingPathComponent("factory/cli.py").path)
            && fm.fileExists(atPath: root.appendingPathComponent("config.local.json").path)
    }

    private static func hasActiveRun(_ root: URL) -> Bool {
        FileManager.default.fileExists(atPath: root.appendingPathComponent("var/live/active-v1-run.json").path)
    }

    private static func availablePort() -> Int? {
        for port in 8790...8890 where isPortAvailable(port) { return port }
        return nil
    }

    private static func isPortAvailable(_ port: Int) -> Bool {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return false }
        defer { Darwin.close(descriptor) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(UInt16(port).bigEndian)
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        return withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
    }
}

private enum LauncherSection: String, CaseIterable, Identifiable {
    case workbench
    case tutorial

    var id: String { rawValue }
    var title: String { self == .workbench ? "工作台" : "使用教程" }
    var symbol: String { self == .workbench ? "square.grid.2x2.fill" : "book.closed.fill" }
}

private struct LauncherWindow: View {
    @EnvironmentObject private var launcher: LauncherModel
    @State private var selection: LauncherSection? = .workbench

    var body: some View {
        NavigationSplitView {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(LauncherSection.allCases) { section in
                    Button {
                        selection = section
                    } label: {
                        Label(section.title, systemImage: section.symbol)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 11)
                            .padding(.vertical, 9)
                            .background(
                                selection == section ? Color.accentColor.opacity(0.14) : .clear,
                                in: RoundedRectangle(cornerRadius: 8, style: .continuous)
                            )
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("navigation-\(section.rawValue)")
                }
                Spacer(minLength: 0)
            }
            .padding(10)
            .navigationTitle("Image Factory")
            .navigationSplitViewColumnWidth(min: 170, ideal: 195, max: 230)
        } detail: {
            Group {
                switch selection ?? .workbench {
                case .workbench: WorkbenchPage()
                case .tutorial: TutorialPage()
                }
            }
            .environmentObject(launcher)
        }
    }
}

private struct WorkbenchPage: View {
    @EnvironmentObject private var launcher: LauncherModel

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            HStack(spacing: 20) {
                appIcon
                    .resizable()
                    .scaledToFit()
                    .frame(width: 86, height: 86)
                    .clipShape(RoundedRectangle(cornerRadius: 19, style: .continuous))
                    .shadow(color: .black.opacity(0.12), radius: 12, y: 6)
                VStack(alignment: .leading, spacing: 7) {
                    Text("图片工作台")
                        .font(.system(size: 30, weight: .bold, design: .rounded))
                    Text("一键打开待审核图片和任务交付页面。")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }
            }

            GroupBox {
                HStack(alignment: .top, spacing: 14) {
                    Image(systemName: statusSymbol)
                        .font(.system(size: 22, weight: .semibold))
                        .foregroundStyle(statusColor)
                        .frame(width: 34)
                    VStack(alignment: .leading, spacing: 5) {
                        Text(launcher.statusTitle)
                            .font(.headline)
                        Text(launcher.message)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Label(launcher.intakeMessage,
                              systemImage: launcher.intakeHasFailure ? "exclamationmark.triangle" : "tray.and.arrow.down")
                            .font(.callout)
                            .foregroundStyle(launcher.intakeHasFailure ? Color.orange : Color.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 7)
            }

            if let projectRoot = launcher.projectRoot {
                Label("已找到 Image-Factory 项目文件夹", systemImage: "folder.fill")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.secondary)
                Text(projectRoot.lastPathComponent)
                    .font(.caption.monospaced())
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            } else {
                Button("选择项目文件夹…", action: launcher.chooseProjectFolder)
                    .buttonStyle(.bordered)
            }

            HStack(spacing: 12) {
                Button(launcher.isRunning ? "重新打开审核工作台" : "启动并打开审核工作台") {
                    launcher.openWorkbench()
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(launcher.isBusy || launcher.projectRoot == nil)

                if launcher.isRunning {
                    Button("停止本机服务", role: .destructive) {
                        launcher.stopService()
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.large)
                }
            }

            Spacer(minLength: 8)
            Divider()
            Label("这里启动本机审图与受信表单接单；接单只排队，不会生成图片。", systemImage: "lock.shield")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text("使用时请保持这个 App 运行。退出 App 会停止它启动的本机服务。")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(34)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var appIcon: Image {
        if let path = Bundle.main.path(forResource: "AppIcon", ofType: "png"),
           let image = NSImage(contentsOfFile: path) {
            return Image(nsImage: image)
        }
        return Image(systemName: "photo.artframe")
    }

    private var statusSymbol: String {
        switch launcher.phase {
        case .stopped: return "power"
        case .starting: return "arrow.triangle.2.circlepath"
        case .running: return "checkmark.circle.fill"
        case .stopping: return "pause.circle"
        case .failed: return "exclamationmark.triangle.fill"
        }
    }

    private var statusColor: Color {
        switch launcher.phase {
        case .running: return .green
        case .failed: return .orange
        case .starting, .stopping: return .blue
        case .stopped: return .secondary
        }
    }
}

private struct TutorialPage: View {
    private let steps: [(String, String, String, String)] = [
        ("1", "打开工作台", "回到“工作台”，点“启动并打开审核工作台”。网页会自动打开。", "macwindow.and.cursorarrow"),
        ("2", "看图并审核", "在“待我审核”里点图片看大图，再选“通过”或“退回”。退回原因可以不填。", "eye"),
        ("3", "领取成品", "审核完成后，流程会自动继续。进入“任务与交付”，点“下载交付”领取 ZIP。", "arrow.down.doc")
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("使用教程")
                        .font(.system(size: 30, weight: .bold, design: .rounded))
                    Text("只要记住三步：打开、看图、下载。")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }

                VStack(spacing: 12) {
                    ForEach(Array(steps.enumerated()), id: \.offset) { _, step in
                        HStack(alignment: .top, spacing: 16) {
                            Text(step.0)
                                .font(.system(size: 17, weight: .bold, design: .rounded))
                                .foregroundStyle(.white)
                                .frame(width: 34, height: 34)
                                .background(Color.accentColor, in: Circle())
                            Image(systemName: step.3)
                                .font(.system(size: 19, weight: .medium))
                                .foregroundStyle(Color.accentColor)
                                .frame(width: 30, height: 34)
                            VStack(alignment: .leading, spacing: 6) {
                                Text(step.1).font(.headline)
                                Text(step.2)
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(18)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.background, in: RoundedRectangle(cornerRadius: 15, style: .continuous))
                        .overlay {
                            RoundedRectangle(cornerRadius: 15, style: .continuous)
                                .stroke(Color(nsColor: .separatorColor).opacity(0.35), lineWidth: 1)
                        }
                    }
                }

                GroupBox("小提示") {
                    VStack(alignment: .leading, spacing: 9) {
                        Label("没有待审核任务时，也能打开空工作台；不会创建任务或生成图片。", systemImage: "info.circle")
                        Label("只有本机配置了商品范围匹配的受信授权，App 才会启动表单接单。", systemImage: "checkmark.shield")
                        Label("如果飞书验证失败，先在飞书登录，再回到 App 重试。", systemImage: "person.crop.circle.badge.exclamationmark")
                        Label("退出 App 会停止本机服务；下次双击 App 再打开即可。", systemImage: "power")
                        Label("重新启动后，请从 App 打开新网页；旧网页地址会失效。", systemImage: "arrow.clockwise")
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 7)
                }
            }
            .padding(34)
            .frame(maxWidth: 760, alignment: .leading)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }
}
