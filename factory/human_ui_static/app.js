(() => {
  'use strict';

  const state = {
    csrfToken: '',
    reviews: [],
    tasks: [],
    loadedViews: new Set(),
    inFlight: new Set(),
    cardByReviewToken: new Map(),
    pendingDecision: null,
  };

  const elements = {
    notice: document.querySelector('#notice'),
    refresh: document.querySelector('#refresh-button'),
    reviewList: document.querySelector('#review-list'),
    reviewState: document.querySelector('#reviews-state'),
    reviewCount: document.querySelector('#review-count'),
    taskList: document.querySelector('#task-list'),
    taskState: document.querySelector('#tasks-state'),
    dialog: document.querySelector('#decision-dialog'),
    dialogForm: document.querySelector('#decision-form'),
    dialogTitle: document.querySelector('#dialog-title'),
    dialogCopy: document.querySelector('#dialog-copy'),
    reasonField: document.querySelector('#reason-field'),
    reasonInput: document.querySelector('#reason-input'),
    confirmDecision: document.querySelector('#confirm-decision'),
    cancelDecision: document.querySelector('#cancel-decision'),
    views: document.querySelectorAll('.view'),
    navLinks: document.querySelectorAll('.nav-link'),
  };

  const text = (value, fallback = '') => typeof value === 'string' && value.trim() ? value.trim() : fallback;

  function payloadData(payload) {
    if (payload && payload.data && typeof payload.data === 'object' && !Array.isArray(payload.data)) return payload.data;
    return payload && typeof payload === 'object' ? payload : {};
  }

  function listFrom(payload, key) {
    const data = payloadData(payload);
    if (Array.isArray(payload)) return payload;
    if (Array.isArray(data[key])) return data[key];
    if (Array.isArray(payload && payload.items)) return payload.items;
    return [];
  }

  function displayOf(item) {
    return item && item.display && typeof item.display === 'object' ? item.display : {};
  }

  function imageUrlOf(display) {
    const value = display.image_url || display.imageUrl || display.image || display.preview_url || display.previewUrl;
    return text(value);
  }

  function safeLink(value) {
    const valueText = text(value);
    if (!valueText) return '';
    try {
      const parsed = new URL(valueText, window.location.href);
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.href;
    } catch (_) {
      return '';
    }
    return '';
  }

  function reviewTokenOf(item) {
    return text(item && (item.review_token || item.reviewToken));
  }

  function showNotice(message, type = '') {
    elements.notice.textContent = message;
    elements.notice.className = `notice${type ? ` is-${type}` : ''}`;
    elements.notice.hidden = false;
  }

  function clearNotice() {
    elements.notice.hidden = true;
    elements.notice.textContent = '';
  }

  function showState(target, title, copy = '') {
    target.classList.remove('has-content');
    target.innerHTML = '';
    const titleNode = document.createElement('p');
    titleNode.className = 'state-title';
    titleNode.textContent = title;
    target.append(titleNode);
    if (copy) {
      const copyNode = document.createElement('p');
      copyNode.className = 'state-copy';
      copyNode.textContent = copy;
      target.append(copyNode);
    }
  }

  function hideState(target) {
    target.classList.add('has-content');
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    // A cold Feishu CLI read may take longer than a normal local request.
    // Match the Engine's configured per-command timeout so first load does not
    // falsely look broken while the read-only candidate list is being fetched.
    const timer = window.setTimeout(() => controller.abort(), 120000);
    try {
      const response = await fetch(path, {
        credentials: 'same-origin',
        ...options,
        signal: options.signal || controller.signal,
      });
      let payload = null;
      try {
        payload = await response.json();
      } catch (_) {
        payload = null;
      }
      if (!response.ok) {
        const error = new Error(text(payloadData(payload).message, `请求失败（${response.status}）`));
        error.status = response.status;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error && error.name === 'AbortError') {
        const timeoutError = new Error('请求超时，正在刷新服务端状态。');
        timeoutError.timeout = true;
        throw timeoutError;
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function loadSession() {
    const payload = await request('/api/v1/session');
    const data = payloadData(payload);
    state.csrfToken = text(data.csrfToken || data.csrf_token || data.csrf);
    if (!state.csrfToken) throw new Error('当前会话缺少安全校验信息，请刷新后重试。');
  }

  async function loadReviews() {
    showState(elements.reviewState, '正在加载待审核图片…');
    try {
      const payload = await request('/api/v1/reviews/pending');
      state.reviews = listFrom(payload, 'reviews').filter((item) => reviewTokenOf(item));
      renderReviews();
      state.loadedViews.add('reviews');
    } catch (error) {
      state.reviews = [];
      elements.reviewCount.textContent = '—';
      showState(elements.reviewState, '暂时无法加载', '请检查网络后刷新页面。');
      showNotice(error.message || '加载失败，请刷新后重试。', 'error');
    }
  }

  async function loadTasks() {
    showState(elements.taskState, '正在加载任务…');
    try {
      const payload = await request('/api/v1/tasks');
      state.tasks = listFrom(payload, 'tasks');
      renderTasks();
      state.loadedViews.add('tasks');
    } catch (error) {
      state.tasks = [];
      showState(elements.taskState, '暂时无法加载', '请检查网络后刷新页面。');
      showNotice(error.message || '加载失败，请刷新后重试。', 'error');
    }
  }

  function renderReviews() {
    elements.reviewList.innerHTML = '';
    state.cardByReviewToken.clear();
    elements.reviewCount.textContent = String(state.reviews.length);
    if (!state.reviews.length) {
      showState(elements.reviewState, '现在没有待审核图片', '有新的图片时，它们会出现在这里。');
      return;
    }
    hideState(elements.reviewState);
    state.reviews.forEach((item) => elements.reviewList.append(createReviewCard(item)));
  }

  function createReviewCard(item) {
    const display = displayOf(item);
    const card = document.createElement('article');
    card.className = 'review-card';

    const imageWrap = document.createElement('div');
    imageWrap.className = 'review-image-wrap';
    const image = document.createElement('img');
    image.className = 'review-image';
    image.alt = text(display.alt, '待审核图片');
    image.loading = 'lazy';
    const imageUrl = safeLink(imageUrlOf(display));
    if (imageUrl) {
      const imageLink = document.createElement('a');
      imageLink.className = 'review-image-link';
      imageLink.href = imageUrl;
      imageLink.target = '_blank';
      imageLink.rel = 'noopener noreferrer';
      imageLink.setAttribute('aria-label', '在新标签页查看候选图片大图');
      imageLink.title = '点击查看大图';
      image.src = imageUrl;
      imageLink.append(image);
      imageWrap.append(imageLink);
      const hint = document.createElement('span');
      hint.className = 'review-image-hint';
      hint.textContent = '点图看大图';
      imageWrap.append(hint);
    } else {
      image.alt = '图片暂不可用';
      imageWrap.classList.add('image-missing');
      imageWrap.append(image);
    }
    card.append(imageWrap);

    const content = document.createElement('div');
    content.className = 'review-content';
    const title = document.createElement('h2');
    title.className = 'review-title';
    title.textContent = text(display.title || display.name, '待审核图片');
    content.append(title);
    const slot = document.createElement('p');
    slot.className = 'review-slot';
    slot.textContent = text(display.slot || display.position, '待确认版位');
    content.append(slot);

    const actions = document.createElement('div');
    actions.className = 'review-actions';
    const approve = makeDecisionButton('通过', 'approve', item);
    const reject = makeDecisionButton('退回', 'reject', item);
    if (item.status === 'processing') {
      approve.disabled = true;
      reject.disabled = true;
      approve.textContent = '正在确认';
    }
    actions.append(approve, reject);
    content.append(actions);
    card.append(content);
    state.cardByReviewToken.set(reviewTokenOf(item), card);
    return card;
  }

  function makeDecisionButton(label, decision, item) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `button ${decision === 'reject' ? 'button-danger' : 'button-primary'}`;
    button.textContent = label;
    button.dataset.decision = decision;
    button.addEventListener('click', () => openDecision(item, decision));
    return button;
  }

  function renderTasks() {
    elements.taskList.innerHTML = '';
    if (!state.tasks.length) {
      showState(elements.taskState, '还没有任务', '任务完成后，交付入口会出现在这里。');
      return;
    }
    hideState(elements.taskState);
    state.tasks.forEach((item) => {
      const display = displayOf(item);
      const card = document.createElement('article');
      card.className = 'task-card';
      const copy = document.createElement('div');
      const title = document.createElement('h2');
      title.className = 'task-title';
      title.textContent = text(display.title || display.name, '未命名任务');
      copy.append(title);
      const status = document.createElement('p');
      status.className = 'task-status';
      status.textContent = text(display.status || display.stage, '处理中');
      copy.append(status);
      card.append(copy);
      const downloadUrl = safeLink(display.download_url || display.downloadUrl || display.delivery_url || display.deliveryUrl);
      if (downloadUrl) {
        const link = document.createElement('a');
        link.className = 'download-link';
        link.href = downloadUrl;
        link.target = '_blank';
        link.rel = 'noopener';
        link.textContent = '下载交付';
        card.append(link);
      }
      elements.taskList.append(card);
    });
  }

  function openDecision(item, decision) {
    const reviewToken = reviewTokenOf(item);
    if (!reviewToken || state.inFlight.has(reviewToken)) return;
    state.pendingDecision = { item, decision, reviewToken };
    const isReject = decision === 'reject';
    elements.dialogTitle.textContent = isReject ? '确认退回这张图片？' : '确认通过这张图片？';
    elements.dialogCopy.textContent = isReject ? '退回后，制作流程会根据系统规则继续处理。' : '确认后，系统会记录你的审核决定并继续流程。';
    elements.reasonField.hidden = !isReject;
    elements.reasonInput.value = '';
    elements.confirmDecision.textContent = isReject ? '确认退回' : '确认通过';
    elements.confirmDecision.className = `button ${isReject ? 'button-danger' : 'button-primary'}`;
    if (typeof elements.dialog.showModal === 'function') elements.dialog.showModal();
    else elements.dialog.setAttribute('open', '');
    if (isReject) elements.reasonInput.focus();
    else elements.confirmDecision.focus();
  }

  function closeDecision() {
    state.pendingDecision = null;
    elements.reasonInput.value = '';
    if (typeof elements.dialog.close === 'function' && elements.dialog.open) elements.dialog.close();
    else elements.dialog.removeAttribute('open');
  }

  function setCardBusy(reviewToken, busy) {
    const card = state.cardByReviewToken.get(reviewToken);
    if (!card) return;
    card.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
  }

  async function submitDecision(item, decision, reason) {
    const reviewToken = reviewTokenOf(item);
    if (!reviewToken || state.inFlight.has(reviewToken)) return;
    state.inFlight.add(reviewToken);
    setCardBusy(reviewToken, true);
    elements.confirmDecision.disabled = true;
    const body = { decision: decision === 'approve' ? 'approved' : 'rejected' };
    const cleanReason = text(reason);
    if (cleanReason) body.reason = cleanReason;
    let outcome = 'unknown';
    try {
      await request(`/api/v1/reviews/${encodeURIComponent(reviewToken)}/decision`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': state.csrfToken,
        },
        body: JSON.stringify(body),
      });
      outcome = 'success';
    } catch (error) {
      outcome = error.status === 409 || error.status === 410 || error.status === 412 ? 'conflict' : 'unknown';
    } finally {
      closeDecision();
      state.inFlight.delete(reviewToken);
      elements.confirmDecision.disabled = false;
      await loadReviews();
    }
    if (outcome === 'success') showNotice('已记录审核决定。', 'success');
    else if (outcome === 'conflict') showNotice('页面已过期或图片已变化，列表已刷新，请以当前状态为准。', 'error');
    else showNotice('提交结果需要确认，列表已刷新，请以当前状态为准。', 'error');
  }

  async function refreshCurrent() {
    clearNotice();
    if (state.loadedViews.has('tasks') && document.querySelector('#tasks-view').hidden === false) await loadTasks();
    else await loadReviews();
  }

  function switchView(viewName) {
    const showReviews = viewName !== 'tasks';
    elements.navLinks.forEach((link) => link.classList.toggle('is-active', link.dataset.view === (showReviews ? 'reviews' : 'tasks')));
    elements.views.forEach((view) => {
      const active = view.dataset.page === (showReviews ? 'reviews' : 'tasks');
      view.hidden = !active;
      view.classList.toggle('is-active', active);
    });
    if (showReviews && !state.loadedViews.has('reviews')) loadReviews();
    if (!showReviews && !state.loadedViews.has('tasks')) loadTasks();
  }

  elements.navLinks.forEach((link) => link.addEventListener('click', () => {
    const viewName = link.dataset.view;
    window.history.replaceState(null, '', `#${viewName}`);
    switchView(viewName);
  }));
  elements.refresh.addEventListener('click', refreshCurrent);
  elements.cancelDecision.addEventListener('click', closeDecision);
  elements.dialogForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!state.pendingDecision) return;
    const { item, decision } = state.pendingDecision;
    submitDecision(item, decision, elements.reasonInput.value);
  });
  elements.dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    closeDecision();
  });

  (async () => {
    try {
      await loadSession();
      switchView(window.location.hash === '#tasks' ? 'tasks' : 'reviews');
    } catch (error) {
      showState(elements.reviewState, '无法打开工作台', error.message || '请刷新后重试。');
      showNotice(error.message || '当前会话不可用，请刷新后重试。', 'error');
      elements.refresh.disabled = false;
    }
  })();
})();
