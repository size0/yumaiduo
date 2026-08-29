const API = Object.freeze({
  overview: 'api/overview',
  samples: 'api/review/samples',
  job: 'api/review/job',
  prelabel: 'api/review/prelabel',
});

const ELEMENT_IDS = [
  'evaluation-refresh', 'evaluation-total', 'evaluation-pending', 'evaluation-labeled', 'evaluation-empty',
  'sample-filter', 'sample-search', 'prelabel-button', 'prelabel-status',
  'prelabel-status-text', 'prelabel-status-detail', 'prelabel-progress',
  'sample-queue-count', 'sample-position', 'sample-list', 'sample-image',
  'sample-image-empty', 'sample-filename', 'sample-meta', 'sample-review-status',
  'sample-review-form', 'review-cinema', 'review-movie', 'review-date',
  'review-showtime', 'review-hall', 'review-seat-type', 'review-seats',
  'review-ticket-count', 'review-original-price', 'review-confidence',
  'prediction-note', 'sample-previous', 'sample-next', 'apply-prediction',
  'sample-save-status', 'reject-sample',
];

export function createEvaluationReviewController({
  authedFetch,
  requestJson,
  notify,
  readableError,
  getMinimumConfidence,
  onOverviewChange,
  formatMetric,
  formatDateTime,
  normalizeSearch,
  setButtonBusy,
  setFormBusy,
  setStatusPill,
  statusLabel,
}) {
  const elements = Object.fromEntries(ELEMENT_IDS.map((id) => [id, document.getElementById(id)]));
  const state = {
    samples: [],
    job: null,
    selectedSampleId: null,
    pollTimer: null,
    imageKey: null,
    imageObjectUrl: null,
    imageRequestId: 0,
  };

  function bindActions() {
    elements['evaluation-refresh'].addEventListener('click', refresh);
    elements['sample-filter'].addEventListener('change', renderSampleReview);
    elements['sample-search'].addEventListener('input', renderSampleReview);
    elements['prelabel-button'].addEventListener('click', startPrelabel);
    elements['sample-review-form'].addEventListener('submit', saveSampleReview);
    elements['reject-sample'].addEventListener('click', rejectCurrentSample);
    elements['sample-previous'].addEventListener('click', () => moveSample(-1));
    elements['sample-next'].addEventListener('click', () => moveSample(1));
    elements['apply-prediction'].addEventListener('click', applyCurrentPrediction);
    elements['review-seat-type'].addEventListener('change', syncSeatFieldRequirements);
    elements['sample-image'].addEventListener('error', handleImageError);
    elements['sample-image'].addEventListener('load', () => {
      elements['sample-image'].hidden = false;
      elements['sample-image-empty'].hidden = true;
    });
    window.addEventListener('beforeunload', destroy, { once: true });
  }

  async function loadData() {
    const [samples, job] = await Promise.all([
      requestJson(API.samples),
      requestJson(API.job),
    ]);
    state.samples = Array.isArray(samples) ? samples : [];
    state.job = job ?? null;
    selectFirstAvailableSample();
    syncPolling();
  }

  function reset() {
    state.samples = [];
    state.job = null;
    state.selectedSampleId = null;
    stopPolling();
    clearSampleImage();
  }

  function render(overview) {
    renderEvaluation(overview);
    renderPrelabelStatus();
    renderSampleReview();
  }

  function renderEvaluation(overview) {
    const evaluation = overview?.sample_evaluation;
    elements['evaluation-total'].textContent = formatMetric(evaluation?.total);
    elements['evaluation-pending'].textContent = formatMetric(evaluation?.pending);
    elements['evaluation-labeled'].textContent = formatMetric(evaluation?.labeled);
    const hasLabeled = Number(evaluation?.labeled) > 0;
    const title = elements['evaluation-empty'].querySelector('strong');
    const copy = elements['evaluation-empty'].querySelector('p');
    title.textContent = hasLabeled ? '已有已确认标签' : '等待人工标签';
    copy.textContent = hasLabeled
      ? `${evaluation.labeled} 个人工标签已确认，满足脱敏许可保留策略后才可正式评测。`
      : '人工标签已确认，满足脱敏许可保留策略后才可正式评测。';
  }

  function selectFirstAvailableSample() {
    if (state.samples.some((sample) => sample.sample_id === state.selectedSampleId)) return;
    state.selectedSampleId = state.samples[0]?.sample_id ?? null;
  }

  function visiblePrediction(sample) {
    if (!sample || (sample.split === 'eval' && sample.review_status !== 'reviewed')) return null;
    return sample.prediction ?? null;
  }

  function filteredSamples() {
    const filter = elements['sample-filter'].value;
    const query = normalizeSearch(elements['sample-search'].value);
    return state.samples.filter((sample) => {
      const prediction = visiblePrediction(sample);
      const matchesFilter = filter === 'all'
        || (filter === 'predicted' && Boolean(prediction))
        || (filter === 'unpredicted' && !prediction)
        || sample.review_status === filter;
      const haystack = normalizeSearch([
        sample.filename, sample.sample_id, sample.cinema_name, sample.movie_name,
        prediction?.cinema_name, prediction?.movie_name,
      ].filter(Boolean).join(' '));
      return matchesFilter && (!query || haystack.includes(query));
    });
  }

  function renderSampleReview() {
    const samples = filteredSamples();
    if (!samples.some((sample) => sample.sample_id === state.selectedSampleId)) {
      state.selectedSampleId = samples[0]?.sample_id ?? null;
    }
    elements['sample-queue-count'].textContent = `${samples.length} 个样本`;
    const selectedIndex = samples.findIndex((sample) => sample.sample_id === state.selectedSampleId);
    elements['sample-position'].textContent = selectedIndex >= 0 ? `${selectedIndex + 1} / ${samples.length}` : '未选择';
    renderSampleList(samples);
    renderSampleEditor(samples[selectedIndex] ?? null, selectedIndex, samples.length);
  }

  function renderSampleList(samples) {
    const list = elements['sample-list'];
    list.replaceChildren();
    if (!samples.length) {
      const empty = document.createElement('div');
      empty.className = 'sample-list-empty';
      empty.textContent = state.samples.length ? '没有符合筛选条件的样本。' : '暂无审核样本。';
      list.append(empty);
      return;
    }
    samples.forEach((sample) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'sample-list-item';
      button.dataset.selected = String(sample.sample_id === state.selectedSampleId);
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', String(sample.sample_id === state.selectedSampleId));
      const copy = document.createElement('span');
      const filename = document.createElement('strong');
      const metadata = document.createElement('small');
      filename.textContent = sample.filename || sample.sample_id;
      metadata.textContent = `${sample.split === 'eval' ? '测试集' : '训练集'} · ${visiblePrediction(sample) ? '已有模型建议' : '无模型建议'}`;
      copy.append(filename, metadata);
      const status = document.createElement('span');
      status.className = `sample-state sample-state-${sample.review_status}`;
      status.textContent = statusLabel(sample.review_status);
      button.append(copy, status);
      button.addEventListener('click', () => {
        state.selectedSampleId = sample.sample_id;
        renderSampleReview();
      });
      list.append(button);
    });
  }

  function renderSampleEditor(sample, selectedIndex, sampleCount) {
    const form = elements['sample-review-form'];
    form.querySelectorAll('button, input, select').forEach((control) => { control.disabled = !sample; });
    elements['sample-previous'].disabled = !sample || selectedIndex <= 0;
    elements['sample-next'].disabled = !sample || selectedIndex >= sampleCount - 1;
    elements['apply-prediction'].disabled = !visiblePrediction(sample);
    if (!sample) {
      clearSampleImage();
      elements['sample-image-empty'].hidden = false;
      elements['sample-image-empty'].textContent = '从左侧选择一张图片开始审核';
      elements['sample-filename'].textContent = '尚未选择样本';
      elements['sample-meta'].textContent = '--';
      setStatusPill(elements['sample-review-status'], 'unknown', '未选择');
      elements['prediction-note'].textContent = '当前没有可审核的样本。';
      form.reset();
      return;
    }

    void loadSampleImage(sample);
    elements['sample-filename'].textContent = sample.filename || sample.sample_id;
    elements['sample-meta'].textContent = `${sample.sample_id} · ${sample.split === 'eval' ? '固定测试集' : '训练集'}`;
    setStatusPill(elements['sample-review-status'], sample.review_status, statusLabel(sample.review_status));
    const prediction = visiblePrediction(sample);
    populateSampleForm(sample.review_status === 'reviewed' ? sample : prediction);
    elements['prediction-note'].textContent = sample.split === 'eval' && sample.review_status !== 'reviewed'
      ? '固定测试集采用盲审，人工确认前不展示或应用模型建议。'
      : prediction
        ? `模型建议：${prediction.model_version || '模型未知'} · 提示词 ${prediction.prompt_version || '版本未知'} · 置信度 ${formatPercent(prediction.confidence)}`
        : '当前样本还没有模型建议，可直接人工填写或先批量预标注。';
    elements['sample-save-status'].textContent = sample.reviewed_at
      ? `最近确认于 ${formatDateTime(sample.reviewed_at)}`
      : '';
  }

  async function loadSampleImage(sample) {
    const nextKey = `${sample.sample_id}:${sample.revision ?? ''}:${sample.image_url ?? ''}`;
    if (state.imageKey === nextKey) return;
    const requestId = ++state.imageRequestId;
    revokeImageObjectUrl();
    state.imageKey = nextKey;
    elements['sample-image'].hidden = true;
    elements['sample-image'].removeAttribute('src');
    elements['sample-image-empty'].hidden = false;
    elements['sample-image-empty'].textContent = '图片加载中...';
    try {
      const response = await authedFetch(imageRequestPath(sample.image_url));
      if (!response || typeof response.blob !== 'function') throw new Error('review_image_invalid_response');
      if (typeof response.ok === 'boolean' && !response.ok) {
        let code = `HTTP ${response.status}`;
        try {
          const payload = await response.json();
          code = payload?.error || code;
        } catch {
          // Keep the HTTP status when the response is not JSON.
        }
        throw new Error(code);
      }
      const blob = await response.blob();
      if (requestId !== state.imageRequestId) return;
      state.imageObjectUrl = URL.createObjectURL(blob);
      elements['sample-image'].src = state.imageObjectUrl;
    } catch (error) {
      if (requestId !== state.imageRequestId) return;
      state.imageKey = null;
      elements['sample-image'].hidden = true;
      elements['sample-image'].removeAttribute('src');
      elements['sample-image-empty'].hidden = false;
      elements['sample-image-empty'].textContent = `图片加载失败：${readableError(error)}`;
    }
  }

  function imageRequestPath(imageUrl) {
    const path = String(imageUrl ?? '');
    if (!/^\/ui\/api\/review\/samples\/[^/]+\/image$/u.test(path)) {
      throw new Error('invalid_review_image_path');
    }
    return path.replace(/^\/ui\//u, '');
  }

  function handleImageError() {
    revokeImageObjectUrl();
    state.imageKey = null;
    elements['sample-image'].hidden = true;
    elements['sample-image'].removeAttribute('src');
    elements['sample-image-empty'].hidden = false;
    elements['sample-image-empty'].textContent = '图片加载失败，请刷新后重试';
  }

  function revokeImageObjectUrl() {
    if (!state.imageObjectUrl) return;
    URL.revokeObjectURL(state.imageObjectUrl);
    state.imageObjectUrl = null;
  }

  function clearSampleImage() {
    state.imageRequestId += 1;
    state.imageKey = null;
    revokeImageObjectUrl();
    elements['sample-image'].hidden = true;
    elements['sample-image'].removeAttribute('src');
  }

  function populateSampleForm(record) {
    const value = record ?? {};
    elements['review-cinema'].value = value.cinema_name ?? '';
    elements['review-movie'].value = value.movie_name ?? '';
    elements['review-date'].value = value.date ?? '';
    elements['review-showtime'].value = value.showtime ?? '';
    elements['review-hall'].value = value.hall_name ?? '';
    elements['review-seat-type'].value = value.seat_type ?? '';
    elements['review-seats'].value = Array.isArray(value.selected_seats) ? value.selected_seats.join('、') : '';
    elements['review-ticket-count'].value = value.ticket_count ?? '';
    elements['review-original-price'].value = Number.isFinite(value.original_unit_price_cents)
      ? (Number(value.original_unit_price_cents) / 100).toFixed(2)
      : '';
    elements['review-confidence'].value = Number.isFinite(value.confidence)
      ? Math.round(value.confidence * 100)
      : '';
    syncSeatFieldRequirements();
  }

  function syncSeatFieldRequirements() {
    const seatType = elements['review-seat-type'].value;
    elements['review-seats'].required = seatType === 'REGULAR';
    elements['review-ticket-count'].required = ['WPLUS', 'REGULAR'].includes(seatType);
    elements['review-original-price'].required = ['WPLUS', 'REGULAR'].includes(seatType);
  }

  function applyCurrentPrediction() {
    const prediction = visiblePrediction(currentSample());
    if (!prediction) return;
    populateSampleForm(prediction);
    elements['sample-save-status'].textContent = '已恢复模型建议，保存前请人工核对。';
  }

  function moveSample(offset) {
    const samples = filteredSamples();
    const currentIndex = samples.findIndex((sample) => sample.sample_id === state.selectedSampleId);
    const target = samples[currentIndex + offset];
    if (!target) return;
    state.selectedSampleId = target.sample_id;
    renderSampleReview();
  }

  function currentSample() {
    return state.samples.find((sample) => sample.sample_id === state.selectedSampleId) ?? null;
  }

  async function saveSampleReview(event) {
    event.preventDefault();
    const sample = currentSample();
    if (!sample || !elements['sample-review-form'].reportValidity()) return;
    await submitSampleReview(sample, buildReviewPayload());
  }

  async function rejectCurrentSample() {
    const sample = currentSample();
    if (!sample) return;
    await submitSampleReview(sample, {
      review_status: 'rejected',
      review_reasons: ['MISSING_REQUIRED_FIELD'],
    });
  }

  function buildReviewPayload() {
    const seatType = elements['review-seat-type'].value;
    const seats = [...new Set(String(elements['review-seats'].value).split(/[\s、,，;；]+/u).map((seat) => seat.trim()).filter(Boolean))];
    const confidence = Number(elements['review-confidence'].value) / 100;
    const reasons = [];
    if (confidence < Number(getMinimumConfidence() ?? 0.9)) reasons.push('LOW_CONFIDENCE');
    if (seatType === 'NONE' || (seatType === 'REGULAR' && seats.length === 0)) reasons.push('SEATS_NOT_SELECTED');
    if (seatType === 'REGULAR' && seats.length !== Number(elements['review-ticket-count'].value)) {
      reasons.push('TICKET_COUNT_MISMATCH');
    }
    return {
      review_status: 'reviewed',
      cinema_name: elements['review-cinema'].value.trim(),
      movie_name: elements['review-movie'].value.trim(),
      date: elements['review-date'].value,
      showtime: elements['review-showtime'].value,
      hall_name: elements['review-hall'].value.trim(),
      seat_type: seatType,
      selected_seats: seats.length ? seats : null,
      ticket_count: elements['review-ticket-count'].value ? Number(elements['review-ticket-count'].value) : null,
      original_unit_price_cents: elements['review-original-price'].value
        ? Math.round(Number(elements['review-original-price'].value) * 100)
        : null,
      confidence,
      review_reasons: reasons,
    };
  }

  async function submitSampleReview(sample, payload) {
    setFormBusy(elements['sample-review-form'], true);
    elements['sample-save-status'].textContent = payload.review_status === 'rejected' ? '正在拒绝...' : '正在保存...';
    try {
      const revision = String(sample.revision ?? '').trim();
      if (!revision) throw new Error('review_revision_required');
      const currentIndex = filteredSamples().findIndex((item) => item.sample_id === sample.sample_id);
      await requestJson(`${API.samples}/${encodeURIComponent(sample.sample_id)}`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json', 'If-Match': revision },
        body: JSON.stringify(payload),
      });
      const overview = await reloadReviewData();
      const remaining = filteredSamples();
      state.selectedSampleId = remaining[Math.min(currentIndex, Math.max(remaining.length - 1, 0))]?.sample_id ?? null;
      onOverviewChange(overview);
      syncPolling();
      notify(payload.review_status === 'rejected' ? '样本已拒绝。' : '人工标签已保存。');
    } catch (error) {
      elements['sample-save-status'].textContent = '保存失败';
      notify(readableError(error), 'error');
    } finally {
      setFormBusy(elements['sample-review-form'], false);
    }
  }

  async function startPrelabel() {
    setButtonBusy(elements['prelabel-button'], true, '正在启动...');
    try {
      state.job = await requestJson(API.prelabel, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: '{}',
      });
      renderPrelabelStatus();
      syncPolling();
      notify(state.job.total ? `已开始预标注 ${state.job.total} 张图片。` : '没有需要预标注的样本。');
    } catch (error) {
      notify(readableError(error), 'error');
    } finally {
      if (state.job?.status !== 'running') setButtonBusy(elements['prelabel-button'], false);
    }
  }

  function renderPrelabelStatus() {
    const job = state.job ?? { status: 'idle', total: 0, completed: 0, failed: 0 };
    const processed = Number(job.completed ?? 0) + Number(job.failed ?? 0);
    const percent = job.total > 0 ? Math.round((processed / job.total) * 100) : 0;
    elements['prelabel-progress'].value = percent;
    elements['prelabel-status'].dataset.status = job.status;
    elements['prelabel-status-text'].textContent = job.status === 'running'
      ? `正在预标注 ${processed} / ${job.total}`
      : job.status === 'idle' ? '预标注尚未启动' : `预标注${statusLabel(job.status)}`;
    elements['prelabel-status-detail'].textContent = job.status === 'running'
      ? `成功 ${job.completed} 张，失败 ${job.failed} 张；已完成结果会即时保存。`
      : job.status === 'idle' ? '配置 AI Key 后可批量处理待审核图片。'
        : `成功 ${job.completed ?? 0} 张，失败 ${job.failed ?? 0} 张。`;
    setButtonBusy(elements['prelabel-button'], job.status === 'running', '预标注中...');
  }

  function stopPolling() {
    if (!state.pollTimer) return;
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  function syncPolling() {
    stopPolling();
    if (state.job?.status === 'running') state.pollTimer = window.setTimeout(pollReviewJob, 2000);
  }

  async function pollReviewJob() {
    try {
      state.job = await requestJson(API.job);
      renderPrelabelStatus();
      if (state.job.status !== 'running') {
        const overview = await reloadReviewData();
        selectFirstAvailableSample();
        onOverviewChange(overview);
        notify(state.job.failed ? '预标注已完成，部分样本需要人工处理。' : '预标注已完成。');
      }
    } catch (error) {
      notify(`刷新预标注进度失败：${readableError(error)}`, 'error');
    } finally {
      syncPolling();
    }
  }

  async function reloadReviewData() {
    const [overview, samples, job] = await Promise.all([
      requestJson(API.overview),
      requestJson(API.samples),
      requestJson(API.job),
    ]);
    state.samples = Array.isArray(samples) ? samples : [];
    state.job = job ?? null;
    return overview;
  }

  async function refresh() {
    setButtonBusy(elements['evaluation-refresh'], true, '刷新中...');
    try {
      const overview = await reloadReviewData();
      selectFirstAvailableSample();
      onOverviewChange(overview);
      syncPolling();
      notify('评测状态已刷新。');
    } catch (error) {
      notify(readableError(error), 'error');
    } finally {
      setButtonBusy(elements['evaluation-refresh'], false);
    }
  }

  function destroy() {
    stopPolling();
    clearSampleImage();
  }

  function formatPercent(value) {
    return Number.isFinite(Number(value)) ? `${Math.round(Number(value) * 100)}%` : '未知';
  }

  return Object.freeze({ bindActions, loadData, render, reset, destroy });
}
