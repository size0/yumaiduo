import { cinemaHintFromSupplement, cityHintFromSupplement } from '../quote-supplement.mjs';

function text(value, maxLength) {
  return String(value ?? '').replace(/\s+/gu, ' ').trim().slice(0, maxLength);
}

export function ticketCountFromText(value) {
  const content = text(value, 1_000);
  const numeric = content.match(/(?:要|需|买|共|一共)?\s*([1-9]|1\d|20)\s*(?:张|个(?:位置|座位)?)/u);
  if (numeric) return Number(numeric[1]);
  if (/(?:要|需|买)\s*一张/u.test(content)) return 1;
  if (/(?:多少钱|好多钱|票价|价格|单价).{0,4}(?:一|每)张|(?:一|每)张.{0,4}(?:多少钱|好多钱|票价|价格|单价)/u.test(content)) return null;
  if (/(?:一张|这一个|一(?:个)?(?:的)?(?:位置|座位))/u.test(content)) return 1;
  if (/(?:两张|两(?:个)?(?:的)?(?:位置|座位))/u.test(content)) return 2;
  if (/(?:三张|三(?:个)?(?:的)?(?:位置|座位))/u.test(content)) return 3;
  // “8排中间两个” is an explicit buyer quantity, unlike a hand-drawn
  // circle. Restrict this shorthand to nearby position language so unrelated
  // prose cannot become an order quantity.
  const contextualCount = content.match(/(?:\d{1,2}\s*排|(?:最)?中间|左边|右边|位置)[^。！？!?]{0,12}?([一二三四五六七八九十两])\s*(?:个)?(?:位置|座位)?(?:[。！？!?]|$)/u);
  if (contextualCount) return ({ 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 })[contextualCount[1]] ?? null;
  // Text seat coordinates establish only count/context, never an official
  // platform selection. This covers forms such as “八排12 13 14”.
  return typedSeats(content).length || null;
}

function isTextQuoteIntent(content) {
  return /(?:多少钱|好多钱|价格|票价|核价|万达|(?:影片|电影|片名)\s*[:：]|\d{1,2}\s*(?:月|[./-])\s*\d{1,2}|(?:[01]?\d|2[0-3])\s*[:：.]\s*[0-5]\d|\d{1,2}\s*排\s*\d{1,2}|\d+\s*张|[一二三四五六七八九十]\s*张)/u.test(content);
}

function relativeTextDate(value, now) {
  const content = String(value ?? '');
  const offset = /(?:后天)/u.test(content) ? 2 : /(?:明天|明日)/u.test(content) ? 1 : /(?:今天|今日)/u.test(content) ? 0 : null;
  if (offset === null) return null;
  const chinaNow = new Date(now.getTime() + 8 * 60 * 60 * 1_000);
  const base = Date.UTC(chinaNow.getUTCFullYear(), chinaNow.getUTCMonth(), chinaNow.getUTCDate() + offset);
  return new Date(base).toISOString().slice(0, 10);
}

function parseTextDate(match, now) {
  if (!match) return null;
  const year = match[1] ? Number(match[1]) : now.getUTCFullYear();
  const month = Number(match[2]);
  const day = Number(match[3]);
  const candidate = new Date(Date.UTC(year, month - 1, day));
  if (candidate.getUTCFullYear() !== year || candidate.getUTCMonth() !== month - 1 || candidate.getUTCDate() !== day) return null;
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  // A date without a year must not silently turn a past request into a quote
  // for another year. Ask the buyer to clarify instead.
  if (!match[1] && candidate.getTime() < today) return null;
  return candidate.toISOString().slice(0, 10);
}

function weekdayDate(content, now) {
  const match = String(content).match(/(?:(下|本|这)\s*)?周\s*([一二三四五六日天])/u);
  if (!match) return null;
  const weekdays = new Map([['日', 0], ['天', 0], ['一', 1], ['二', 2], ['三', 3], ['四', 4], ['五', 5], ['六', 6]]);
  const target = weekdays.get(match[2]);
  if (target === undefined) return null;
  const current = now.getUTCDay();
  let offset = (target - current + 7) % 7;
  if (match[1] === '下') offset += 7;
  const date = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + offset));
  return date.toISOString().slice(0, 10);
}

function chineseNumber(value) {
  const digits = new Map([['一', 1], ['二', 2], ['三', 3], ['四', 4], ['五', 5], ['六', 6], ['七', 7], ['八', 8], ['九', 9]]);
  const raw = String(value ?? '');
  if (raw === '十') return 10;
  if (raw.includes('十')) {
    const [tensRaw, onesRaw] = raw.split('十');
    const tens = tensRaw ? digits.get(tensRaw) : 1;
    const ones = onesRaw ? digits.get(onesRaw) : 0;
    return Number.isInteger(tens) && Number.isInteger(ones) ? tens * 10 + ones : null;
  }
  return digits.get(raw) ?? null;
}

function normalizeSeatRows(content) {
  return String(content ?? '').replace(/([一二三四五六七八九十]{1,3})\s*(?:排|行)/gu, (match, value) => {
    const row = chineseNumber(value);
    return row && row <= 99 ? `${row}排` : match;
  });
}

function typedSeats(content) {
  const normalized = normalizeSeatRows(content);
  const seats = [];
  const add = (row, column) => {
    const seat = `${Number(row)}排${Number(column)}座`;
    if (!seats.includes(seat)) seats.push(seat);
  };
  // A bounded range such as “9排18-19” explicitly supplies two typed seat
  // positions. It establishes count/context only, never official selection.
  for (const match of normalized.matchAll(/(\d{1,2})\s*排\s*(\d{1,2})\s*(?:-|—|~|～|至|到)\s*(\d{1,2})\s*(?:座|号)?/gu)) {
    const start = Number(match[2]);
    const end = Number(match[3]);
    if (end >= start && end - start < 20) {
      for (let column = start; column <= end; column += 1) add(match[1], column);
    }
  }
  for (const match of normalized.matchAll(/(\d{1,2})\s*排\s*(\d{1,2})\s*座?/gu)) add(match[1], match[2]);
  // Accept buyer shorthand such as “7排5.6.7号”; it supplies context/count
  // only and is never treated as official platform-selected seats.
  for (const match of normalized.matchAll(/(\d{1,2})\s*排\s*((?:\d{1,2}\s*(?:[、,.，]|\s+)\s*)+\d{1,2})(?!\s*排)\s*(?:座|号)?/gu)) {
    for (const column of match[2].matchAll(/\d{1,2}/gu)) add(match[1], column[0]);
  }
  for (const match of normalized.matchAll(/(\d{1,2})\s*排\s*(\d{1,2})(?:座|号)?((?:\s*(?:[、,.，]|\s+)\s*\d{1,2}(?:座|号)?(?!\s*排))+)/gu)) {
    add(match[1], match[2]);
    for (const column of match[3].matchAll(/\d{1,2}/gu)) add(match[1], column[0]);
  }
  return seats;
}

function missingFactsReply(missing) {
  return `为了实时核价，请发送已标记需要购买位置的完整选座页截图，并说明需要几张。截图需要补全：${missing.join('、')}。图片标记仅供人工出票，不代表官方选座。`;
}

export function semanticQuoteFacts(response, { hasImage = false } = {}) {
  const facts = response?.status === 'extracted' && response.facts && typeof response.facts === 'object' && !Array.isArray(response.facts)
    ? response.facts
    : null;
  const confidence = Number(facts?.confidence);
  if (!facts || facts.quote_intent !== true || !Number.isFinite(confidence) || confidence < 0.8 || confidence > 1) return null;
  const date = /^\d{4}-\d{2}-\d{2}$/u.test(String(facts.date ?? '')) ? String(facts.date) : '';
  const showtime = /^\d{2}:\d{2}$/u.test(String(facts.showtime ?? '')) ? String(facts.showtime) : '';
  const seats = Array.isArray(facts.seat_numbers)
    ? [...new Set(facts.seat_numbers.map((value) => String(value).replace(/\s+/gu, '')).filter((value) => /^\d{1,2}排\d{1,3}座$/u.test(value)))].slice(0, 20)
    : [];
  const ticketCount = Number(facts.ticket_count);
  const recognition = {
    image_type: 'UNKNOWN',
    ...(text(facts.city, 80) ? { city: text(facts.city, 80) } : {}),
    ...(text(facts.cinema, 160) ? { cinema: text(facts.cinema, 160) } : {}),
    ...(text(facts.movie, 160) ? { movie: text(facts.movie, 160) } : {}),
    ...(date ? { date } : {}), ...(showtime ? { showtime } : {}),
    ...(text(facts.hall, 80) ? { hall: text(facts.hall, 80) } : {}),
    official_selection: { is_selected: false, selected_seat_numbers: seats, selected_count: 0 },
  };
  const requestedRow = Number(facts.requested_row);
  const missing = [
    !recognition.date && '日期（请使用未过期日期；过去日期请带年份）',
    !recognition.cinema && '完整万达影院名',
    !recognition.showtime && '开场时间',
    !recognition.movie && '影片名',
    facts.refers_to_image_positions === true && !hasImage && seats.length === 0 && '这几个位置的完整选座截图',
  ].filter(Boolean);
  const fieldSources = Object.fromEntries([
    ...['city', 'cinema', 'movie', 'date', 'showtime', 'hall'].map((name) => [name, recognition[name] ? 'ai_text' : '']).filter(([, source]) => source),
    ...(Number.isInteger(ticketCount) && ticketCount >= 1 && ticketCount <= 20 ? [['ticket_count', 'ai_text']] : []),
    ...(Number.isInteger(requestedRow) && requestedRow >= 1 && requestedRow <= 99 ? [['requested_row', 'ai_text']] : []),
  ]);
  const result = {
    status: missing.length ? 'needs_confirmation' : 'recognized', text_quote: true,
    ticket_count: Number.isInteger(ticketCount) && ticketCount >= 1 && ticketCount <= 20 ? ticketCount : (seats.length || null),
    recognition, field_sources: fieldSources,
    ...(Number.isInteger(requestedRow) && requestedRow >= 1 && requestedRow <= 99 ? { requested_row: requestedRow } : {}),
    semantic_source: text(response.extractor_version, 100) || 'ai',
  };
  return Object.freeze(missing.length
    ? { ...result, failure_code: 'text_quote_missing_fields', missing_fields: missing, reply_text: missingFactsReply(missing) }
    : result);
}

const PROVINCE_PREFIXES = Object.freeze([
  '黑龙江', '内蒙古', '广西', '宁夏', '新疆', '西藏',
  '北京', '天津', '上海', '重庆', '河北', '山西', '辽宁', '吉林', '江苏', '浙江',
  '安徽', '福建', '江西', '山东', '河南', '湖北', '湖南', '广东', '海南', '四川',
  '贵州', '云南', '陕西', '甘肃', '青海', '台湾', '香港', '澳门',
]);

function cityFromCompactLocation(value) {
  const location = String(value ?? '').replace(/\s+/gu, '').replace(/(?:省|自治区)$/u, '');
  const province = PROVINCE_PREFIXES.find((item) => location.startsWith(item) && location.length > item.length);
  return (province ? location.slice(province.length) : location).replace(/市$/u, '');
}

// Parse only explicit, bounded facts from buyer text. Typed seat numbers are
// useful for quantity/context, but are never presented to Wanda as official
// platform-selected seats.
export function parseTextQuoteRequest(value, now = new Date()) {
  // Keep line boundaries: buyers often send cinema and movie on separate
  // lines, and those boundaries disambiguate a cinema name from a film title.
  const content = String(value ?? '').replace(/\r\n?/gu, '\n').trim().slice(0, 1_000);
  if (!isTextQuoteIntent(content)) return Object.freeze({ status: 'ignored_no_image' });
  const compactShowtime = content.match(/^(?<location>[\u4e00-\u9fff]{2,12})\s+(?<cinema>[\u4e00-\u9fffA-Za-z0-9（）()·]{2,40}万达(?:影城|影院)?)\s+(?<date>(?:\d{4}[年./-])?\d{1,2}[月./-]\d{1,2}日?)(?<movie>[\u4e00-\u9fffA-Za-z0-9·：:]{1,60}?)(?<start>(?:[01]?\d|2[0-3])[:：∶.]\d{2})\s*(?:-|—|~|～|至|到)\s*(?<end>(?:[01]?\d|2[0-3])[:：∶.]\d{2})/u);
  const dateMatch = content.match(/(?:(\d{4})\s*[年./-]\s*)?(\d{1,2})\s*(?:月|[./-])\s*(\d{1,2})\s*日?/u);
  const date = parseTextDate(dateMatch, now) ?? relativeTextDate(content, now) ?? weekdayDate(content, now);
  const dateStart = Number(dateMatch?.index ?? -1);
  const dateEnd = dateStart + String(dateMatch?.[0] ?? '').length;
  const timeMatches = [...content.matchAll(/((?:[01]?\d|2[0-3]))\s*[:：∶.]\s*([0-5]\d)/gu)];
  const timeMatch = timeMatches.find((match) => {
    const start = Number(match.index ?? -1);
    return start < dateStart || start >= dateEnd;
  }) ?? null;
  const timeStart = Number(timeMatch?.index ?? -1);
  const cinemaBeforeDate = dateMatch ? text(content.slice(0, dateStart).replace(/[，,。；;、\s]+$/u, ''), 160) : '';
  const cinemaBetweenDateAndTime = dateMatch && timeMatch && dateEnd <= timeStart
    ? text(content.slice(dateEnd, timeStart).replace(/^(?:晚上|下午|中午|早上|凌晨|那场|的|，|,|。|\s)+/u, ''), 160)
    : '';
  const lines = content.split(/\r?\n/u).map((line) => text(line, 160)).filter(Boolean);
  const namedCinema = text(content.match(/(?:[\u4e00-\u9fff]{1,12})?(?:万达影城|万达影院)(?:[（(][^）)\n]{1,80}[）)])?/u)?.[0], 160);
  const labelled = (labels) => text(lines.find((line) => new RegExp(`^(?:${labels})\\s*[:：]`, 'u').test(line))?.replace(new RegExp(`^(?:${labels})\\s*[:：]\\s*`, 'u'), ''), 160);
  const structuredCinema = labelled('影院|影城|门店') || (lines.length > 1 ? lines.find((line) => line.includes('万达')) ?? '' : '');
  let cinema = text(compactShowtime?.groups?.cinema, 160) || structuredCinema || cinemaHintFromSupplement(content) || cinemaBeforeDate || cinemaBetweenDateAndTime || namedCinema;
  const city = cityFromCompactLocation(compactShowtime?.groups?.location) || cityHintFromSupplement(content);
  const seats = typedSeats(content);
  const firstSeat = content.search(/\d{1,2}\s*排\s*\d{1,2}\s*座?/u);
  const referencedSeats = content.search(/(?:这|那)?(?:[1-9]|1\d|20|[一二两三四五六七八九十]{1,3})\s*(?:个)?(?:位置|座位)/u);
  const priceQuestion = content.search(/(?:多少钱|价格|票价|核价|还有票|有票吗|\d+\s*张|[一二三四五六七八九十]+\s*张|老板)/u);
  const movieEnd = [firstSeat, referencedSeats, priceQuestion].filter((index) => index >= 0 && index > timeStart).sort((left, right) => left - right)[0] ?? content.length;
  const namedCinemaIndex = namedCinema ? content.indexOf(namedCinema) : -1;
  const movieStart = timeMatch ? timeStart + timeMatch[0].length : namedCinemaIndex >= 0 ? namedCinemaIndex + namedCinema.length : -1;
  const movieRaw = movieStart >= 0 ? content.slice(movieStart, movieEnd) : '';
  const inlineHall = text(content.match(/第?\s*\d{1,2}\s*号?\s*(?:放映厅|影厅|厅)/u)?.[0].replace(/\s+/gu, ''), 80);
  let movie = text(movieRaw
    .replace(/^(?:开场的?|那场|的|场|，|,|。|\s)+/u, '')
    // In compact buyer text such as “19:10影片名 7号厅6排8座”,
    // the hall is a separate fact, never part of the film title.
    .replace(/\s*第?\s*\d{1,2}\s*号?\s*(?:放映厅|影厅|厅).*$/u, '')
    .replace(/(?:imax|i-max|3d|2d|普通话|国语|英语|中文|老板|多少钱|价格|票价|核价).*$/iu, ''), 160);
  const structuredMovie = labelled('影片|电影|片名') || (lines.length > 1 ? lines.find((line) => (
    !line.includes('万达')
    && !/^(?:影厅|影院|影城|门店|场次|座位|票数|地址)\s*[:：]/u.test(line)
    && !/(?:周[一二三四五六日天]|\d{1,2}\s*月\s*\d{1,2}|(?:[01]?\d|2[0-3])\s*[:：∶.]\s*[0-5]\d|\d{1,2}\s*排)/u.test(line)
    && line.length >= 2
  )) : '');
  if (structuredMovie) movie = structuredMovie;
  if (compactShowtime?.groups?.movie) movie = text(compactShowtime.groups.movie, 160);
  const hall = labelled('影厅|放映厅') || inlineHall;
  const referencedSeatImageRequired = referencedSeats >= 0 && seats.length === 0;
  const missing = [
    !date && '日期（请使用未过期日期；过去日期请带年份）',
    !cinema && '完整万达影院名',
    !timeMatch && (/(?:[01]?\d|2[0-3])\s*点\s*(?:多|左右)/u.test(content) ? '准确开场时间（“9点多”请补为如9:20）' : '开场时间'),
    !movie && '影片名',
    referencedSeatImageRequired && '这几个位置的完整选座截图',
  ].filter(Boolean);
  const ticketCount = ticketCountFromText(content) ?? (seats.length || null);
  const recognition = {
    image_type: 'UNKNOWN', ...(city ? { city } : {}), ...(cinema ? { cinema } : {}), ...(movie ? { movie } : {}), ...(date ? { date } : {}),
    ...(timeMatch ? { showtime: `${String((/(?:晚上|夜场|晚)/u.test(content.slice(Math.max(0, timeStart - 6), timeStart)) && Number(timeMatch[1]) < 12) ? Number(timeMatch[1]) + 12 : Number(timeMatch[1])).padStart(2, '0')}:${timeMatch[2]}` } : {}),
    ...(hall ? { hall } : {}),
    official_selection: { is_selected: false, selected_seat_numbers: seats, selected_count: 0 },
  };
  const fieldSources = Object.fromEntries([
    ...['city', 'cinema', 'movie', 'date', 'showtime', 'hall'].map((name) => [name, recognition[name] ? 'buyer_text' : '']).filter(([, source]) => source),
    ...(ticketCount ? [['ticket_count', 'buyer_text']] : []),
  ]);
  if (missing.length) return Object.freeze({
    status: 'needs_confirmation', failure_code: 'text_quote_missing_fields', missing_fields: missing,
    reply_text: missingFactsReply(missing), ticket_count: ticketCount, recognition, field_sources: fieldSources,
  });

  return Object.freeze({ status: 'recognized', text_quote: true, ticket_count: ticketCount, recognition, field_sources: fieldSources });
}
