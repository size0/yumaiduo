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

function yuanText(cents) {
  const amount = Number(cents);
  return Number.isSafeInteger(amount) && amount > 0 ? (amount / 100).toFixed(2) : '';
}

export function textQuoteReply(quote, recognized) {
  const unit = Number(quote?.unit_quote_cents);
  const total = Number(quote?.total_quote_cents);
  const count = Number(quote?.ticket_count ?? recognized?.ticket_count);
  if (!Number.isSafeInteger(unit) || unit <= 0) return '';
  const cinema = text(quote?.matched_cinema_name ?? recognized?.recognition?.cinema, 160);
  const base = `${cinema ? `${cinema}：` : ''}按您提供的场次依据万达实时座位图与当前规则核到单价：${yuanText(unit)}元/张`;
  if (Number.isInteger(count) && count > 0 && Number.isSafeInteger(total) && total > 0) {
    return `${base}，${count}张合计${yuanText(total)}元。文字里的座位号未按平台选座核验，请以官方选座图为准。`;
  }
  return `${base}。请确认需要几张；文字里的座位号未按平台选座核验，请补发官方选座图。`;
}

export function recognitionReplyText(recognition) {
  const fields = [
    ['影院', recognition?.cinema], ['影片', recognition?.movie], ['日期', recognition?.date],
    ['场次', recognition?.showtime], ['影厅', recognition?.hall],
  ].map(([label, value]) => value ? `${label}：${text(value, 160)}` : '').filter(Boolean);
  const seats = Array.isArray(recognition?.official_selection?.selected_seat_numbers)
    ? recognition.official_selection.selected_seat_numbers.map((seat) => text(seat, 40)).filter(Boolean) : [];
  if (seats.length) fields.push(`座位：${seats.join('、')}`);
  const count = Number(recognition?.official_selection?.selected_count);
  if (Number.isInteger(count) && count > 0) fields.push(`张数：${count}`);
  if (!fields.length) return '已识别到选座图，正在按当前信息查询万达实时价格。';
  return `已识别：\n${fields.join('\n')}\n正在按万达实时价格查询。`;
}

export function backendQuoteReplyText(quote) {
  return multilineText(quote?.reply_text, 500);
}

export function quoteReplyText(quote, recognition = null) {
  const seatQuotes = Array.isArray(quote?.seat_quotes) ? quote.seat_quotes : [];
  const total = Number(quote?.total_quote_cents);
  if (seatQuotes.length && Number.isSafeInteger(total) && total > 0) {
    const details = seatQuotes.map((item) => `${text(item?.seat_number, 24)} ${yuanText(item?.unit_quote_cents)}元`).filter((item) => !item.endsWith(' 元'));
    return `按官方选座逐座实时核验：${details.join('、')}；${seatQuotes.length}张合计${yuanText(total)}元。`;
  }
  const unit = Number(quote?.unit_quote_cents);
  if (!Number.isSafeInteger(unit) || unit <= 0) throw new Error('quote preview quote returned an invalid unit price');
  const ticketCount = Number(quote?.ticket_count);
  const unitText = `${yuanText(unit)}元/张`;
  const cinema = text(quote?.matched_cinema_name ?? recognition?.cinema, 160);
  const prefix = `${cinema ? `${cinema}：` : ''}万达实时座位图与当前规则报价：${unitText}`;
  if (Number.isInteger(ticketCount) && ticketCount > 0 && Number.isSafeInteger(total) && total > 0) {
    return `${prefix}，${ticketCount}张合计${yuanText(total)}元。`;
  }
  return `${prefix}，请确认需要几张，我再核对合计。`;
}
