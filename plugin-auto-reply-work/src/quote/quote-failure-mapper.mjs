function text(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

function multilineText(value, maxLength) {
  return String(value ?? '')
    .replace(/\r\n?/gu, '\n')
    .replace(/[^\S\n]+/gu, ' ')
    .replace(/ *\n */gu, '\n')
    .replace(/\n{3,}/gu, '\n\n')
    .trim()
    .slice(0, maxLength);
}

export function safeMatchCandidate(candidate, original) {
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return null;
  const result = {};
  for (const field of ['city', 'cinema', 'movie', 'date', 'showtime', 'hall']) {
    const value = text(candidate[field], field === 'date' || field === 'showtime' ? 32 : 160);
    if (value && value !== text(original?.[field], field === 'date' || field === 'showtime' ? 32 : 160)) result[field] = value;
  }
  return Object.keys(result).length ? result : null;
}

export function quoteFailure(detail, status) {
  const structured = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail : null;
  const explicitCode = text(structured?.code, 100);
  const message = text(structured?.message ?? detail, 500);
  const initialCode = explicitCode || quoteFailureCode(message, status);
  const zeroMatches = Number(structured?.diagnostics?.match?.result_count) === 0;
  const code = initialCode === 'showtime_not_unique' && zeroMatches ? 'showtime_not_found' : initialCode;
  const diagnostics = safeQuoteDiagnostics(structured?.diagnostics, code);
  return { code, diagnostics, replyText: multilineText(structured?.reply_text, 500) };
}

function quoteFailureCode(message, status) {
  if (message.includes('官方影院库')) return 'cinema_catalog_not_unique';
  if (message.includes('官方已选座') && message.includes('实时')) return 'official_selection_unverifiable';
  if (message.includes('账号池') || (message.includes('W+') && message.includes('次数'))) return 'wplus_account_unavailable';
  if (message.includes('W+') && (message.includes('优惠') || message.includes('会员价'))) return 'wplus_price_unavailable';
  if (message.includes('座位') && (message.includes('不足') || message.includes('可用'))) return 'insufficient_available_seats';
  if (message.includes('场次') || message.includes('匹配')) return 'showtime_not_unique';
  return Number(status) >= 500 ? 'wanda_gateway_unavailable' : 'quote_verification_failed';
}

function safeQuoteDiagnostics(value, code) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const requested = value.requested_match && typeof value.requested_match === 'object' && !Array.isArray(value.requested_match) ? value.requested_match : null;
  const requestedMatch = requested ? Object.freeze({
    city: text(requested.city, 80), cinema: text(requested.cinema, 160), movie: text(requested.movie, 160),
    date: text(requested.date, 10), showtime: text(requested.showtime, 5), hall: text(requested.hall, 80),
  }) : null;
  const match = value.match && typeof value.match === 'object' && !Array.isArray(value.match) ? value.match : {};
  const areas = Array.isArray(value.realtime_areas) ? value.realtime_areas.slice(0, 20).map((area) => ({
    area_code: text(area?.area_code, 80), label: text(area?.label, 80),
    sales_price_cents: Number.isSafeInteger(area?.sales_price_cents) ? area.sales_price_cents : null,
    wplus_member_price_cents: Number.isSafeInteger(area?.wplus_member_price_cents) ? area.wplus_member_price_cents : null,
    available_seat_count: Number.isSafeInteger(area?.available_seat_count) ? area.available_seat_count : null,
  })) : [];
  return Object.freeze({
    failure_step: text(value.failure_step, 80), safe_error_code: text(value.safe_error_code, 100) || code,
    upstream_status: Number.isInteger(value.upstream_status) ? value.upstream_status : null,
    ...(requestedMatch ? { requested_match: requestedMatch } : {}),
    match: Object.freeze({ result_count: Number.isInteger(match.result_count) ? match.result_count : null, cinema: text(match.cinema, 160), showtime: text(match.showtime, 80) }),
    realtime_areas: Object.freeze(areas),
  });
}

export function quoteFailureReplyText(code, recognition = null) {
  if (code === 'cinema_catalog_not_unique') {
    const cinema = text(recognition?.cinema, 160);
    if (!recognition?.city && cinema) return `已识别到${cinema}，请补充所在城市，我会保留当前影片和场次信息继续实时核验，无需重发截图。`;
    return '该影院暂未在官方影院库唯一匹配，请核对所在城市和完整官方影院名；已识别的影片和场次信息会继续保留。';
  }
  if (code === 'showtime_not_found' || code === 'showtime_not_unique') {
    const missing = [
      !text(recognition?.cinema, 160) && '完整万达影院名',
      !text(recognition?.movie, 160) && '影片名',
      !text(recognition?.date, 32) && '日期',
      !text(recognition?.showtime, 32) && '开场时间',
    ].filter(Boolean);
    if (missing.length) {
      const instruction = missing.length === 1 && missing[0] === '影片名' ? '请直接回复“影片：完整影片名”。' : '';
      return `暂未唯一匹配到官方实时场次，还缺：${missing.join('、')}。${instruction}`;
    }
    if (code === 'showtime_not_found') return '当前万达官方场次中未找到该日期和开场时间，请刷新万达选座页后发送最新截图。';
    return '暂未唯一匹配到官方实时场次，请确认影片名称是否有错字，或补发场次顶部截图。';
  }
  if (code === 'official_selection_unverifiable') return '截图中的官方已选座当前并非全部实时可选，请在购票平台重新选择当前可选座位后发送最新完整截图；请先不要付款。';
  if (code === 'temporary_lock_release_unverified') return '本次临时试价已尝试取消，但试价座位尚未在万达实时座位图中确认恢复；这不代表该场会员座都不可售。为避免重复占座，本次已停止自动报价，请勿付款，稍后重新发送最新完整选座页。';
  if (code === 'wplus_area_unavailable') return '截图位置无法唯一对应当前 W+ 区域，已记录为人工出票位置偏好，请人工确认后处理；不锁座，余票以出票时为准。';
  if (code === 'wplus_price_unavailable') return '该场暂未查到可用的 W+ 会员优惠，请确认是否更换场次或座区。';
  if (code === 'quote_price_conflict') return '当前场次会员优惠不足，按当前规则暂无法形成安全报价，请人工确认。';
  if (code === 'insufficient_available_seats') return '该场当前未查到可用于 W+ 实时核价的座位，可能余票不足或场次已变化；请换场次或刷新官方选座图后重新发送。';
  if (code === 'wanda_gateway_unavailable') return '万达实时核价暂不可用，请稍后重试，无需重复发送全部信息。';
  return '暂未核到该场实时价格，请补充所在城市、完整影院名、影片和开场时间。';
}
