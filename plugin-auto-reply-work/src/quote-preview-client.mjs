import { createHash } from 'node:crypto';
import { cinemaHintFromSupplement, cityHintFromSupplement } from './quote-supplement.mjs';

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

function buyerLabel(payload) {
  const nick = text(payload?.buyerNick ?? payload?.buyer_nick ?? payload?.buyerName, 48);
  if (!nick) return '匿名买家';
  if (nick.length <= 2) return `${nick[0]}*`;
  return `${nick[0]}${'*'.repeat(Math.min(4, nick.length - 2))}${nick.at(-1)}`;
}

async function importImageForRecognition(sourceUrl, envelope, imageLoader, backendClient, loadedImage = null) {
  // IM image URLs can be short-lived or require CDN-specific request handling.
  // Import once through the plugin's SSRF-protected loader, then let V3 read the
  // stable COS object instead of downloading the buyer's original URL again.
  if (!imageLoader || !backendClient) return sourceUrl;
  const image = loadedImage ?? await imageLoader.load(sourceUrl);
  const uploaded = await backendClient.uploadTestImage({
    bytes: image.bytes,
    contentType: image.contentType,
    filename: `inbound-${text(envelope?.id, 80) || 'image'}`,
  }, {
    tenantId: text(envelope?.tenantId, 128),
    eventId: text(envelope?.id, 128),
  });
  const imageUrl = text(uploaded?.url, 2_000);
  if (!imageUrl) throw new Error('image import did not return a COS URL');
  return imageUrl;
}

function ticketCountFromText(value) {
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

function recognitionImageUrls(payload) {
  const values = Array.isArray(payload?.imageUrls) ? payload.imageUrls : [];
  return [...new Set(values.filter((item) => typeof item === 'string' && item.trim()).map((item) => item.trim()))].slice(-2);
}

function firstImageUrl(payload) {
  const value = recognitionImageUrls(payload)[0];
  if (value) return value.trim();

  const textUrl = text(payload?.content ?? payload?.text, 2_000);
  try {
    const parsed = new URL(textUrl);
    if (!['http:', 'https:'].includes(parsed.protocol)) return null;
    if (/\.(?:jpe?g|png|webp)(?:$|[?#])/iu.test(parsed.pathname)) return parsed.toString();
  } catch {
    // The message is normal text rather than an image URL.
  }
  return null;
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

function semanticQuoteFacts(response, { hasImage = false } = {}) {
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

function presentText(value) {
  return typeof value === 'string' && value.trim() ? value : null;
}

function mergeRecognitionWithTextFacts(recognition, parsedText, content = '') {
  const textRecognition = parsedText?.status === 'recognized' && parsedText?.recognition
    ? parsedText.recognition
    : {};
  // Explicit bounded location supplements may fill facts omitted by the
  // screenshot. They never alter platform, prices, or official seat selection.
  const city = presentText(recognition?.city) ?? presentText(textRecognition.city) ?? presentText(cityHintFromSupplement(content));
  const imageCinema = presentText(recognition?.cinema);
  const cinemaHint = presentText(cinemaHintFromSupplement(content));
  const genericImageCinema = /^(?:万达|万达影城|万达影院)$/u.test(String(imageCinema ?? '').replace(/[（）()\s]/gu, ''));
  const cinema = imageCinema && !genericImageCinema
    ? imageCinema
    : presentText(textRecognition.cinema) ?? cinemaHint ?? imageCinema;
  return {
    ...recognition,
    ...(city ? { city } : {}),
    ...(cinema ? { cinema } : {}),
    ...(presentText(recognition?.movie) ?? presentText(textRecognition.movie) ? { movie: presentText(recognition?.movie) ?? textRecognition.movie } : {}),
    ...(recognition?.date ?? textRecognition.date ? { date: recognition?.date ?? textRecognition.date } : {}),
    ...(presentText(recognition?.showtime) ?? presentText(textRecognition.showtime) ? { showtime: presentText(recognition?.showtime) ?? textRecognition.showtime } : {}),
    ...(presentText(recognition?.hall) ?? presentText(textRecognition.hall) ? { hall: presentText(recognition?.hall) ?? textRecognition.hall } : {}),
  };
}

function quoteDraftFacts(value) {
  const fields = value?.fields && typeof value.fields === 'object' && !Array.isArray(value.fields) ? value.fields : {};
  const artifact = value?.recognition_artifact?.recognition;
  const artifactRecognition = artifact && typeof artifact === 'object' && !Array.isArray(artifact) ? artifact : {};
  const field = (name, maximum) => {
    const item = fields[name];
    const raw = item && typeof item === 'object' && !Array.isArray(item) ? item.value : '';
    return text(raw || artifactRecognition[name], maximum);
  };
  const officialSelection = artifactRecognition.official_selection && typeof artifactRecognition.official_selection === 'object'
    && !Array.isArray(artifactRecognition.official_selection) ? artifactRecognition.official_selection : null;
  const storedTicketCount = Number(fields.ticket_count?.value);
  const officialTicketCount = officialSelection?.is_selected === true
    ? Number(officialSelection.selected_count || officialSelection.selected_seat_numbers?.length)
    : null;
  const ticketCount = Number.isInteger(storedTicketCount) && storedTicketCount >= 1 && storedTicketCount <= 20
    ? storedTicketCount
    : Number.isInteger(officialTicketCount) && officialTicketCount >= 1 && officialTicketCount <= 20 ? officialTicketCount : null;
  return {
    recognition: {
      ...artifactRecognition,
      ...(field('city', 80) ? { city: field('city', 80) } : {}),
      ...(field('cinema', 160) ? { cinema: field('cinema', 160) } : {}),
      ...(field('movie', 160) ? { movie: field('movie', 160) } : {}),
      ...(field('date', 32) ? { date: field('date', 32) } : {}),
      ...(field('showtime', 32) ? { showtime: field('showtime', 32) } : {}),
      ...(field('hall', 80) ? { hall: field('hall', 80) } : {}),
      ...(officialSelection ? { official_selection: officialSelection } : {}),
    },
    ticket_count: ticketCount,
    field_sources: Object.fromEntries(['city', 'cinema', 'movie', 'date', 'showtime', 'hall', 'ticket_count'].map((name) => [name, text(fields[name]?.source, 40) || (name === 'ticket_count' && officialTicketCount ? 'image' : 'conversation_draft')]).filter(([name, source]) => name === 'ticket_count' ? ticketCount : source !== 'conversation_draft' || Boolean(field(name, name === 'hall' ? 80 : 160)))),
  };
}

function timeSupplement(value) {
  const match = String(value ?? '').match(/((?:[01]?\d|2[0-3]))\s*[:：∶.]\s*([0-5]\d)/u);
  return match ? `${String(match[1]).padStart(2, '0')}:${match[2]}` : '';
}

function movieSupplement(value) {
  const match = String(value ?? '').match(/(?:^|\n)\s*(?:影片|电影|片名)\s*[:：]\s*([^\n]{1,160})/u);
  return text(match?.[1], 160);
}

function textFactsWithQuoteDraft(parsedText, quoteDraft, content) {
  const draft = quoteDraftFacts(quoteDraft);
  const parsed = parsedText?.status === 'recognized' ? parsedText : null;
  const city = cityHintFromSupplement(content);
  const cinemaSupplement = cinemaHintFromSupplement(content);
  const existingCinema = parsed?.recognition?.cinema || draft.recognition.cinema;
  const genericCinema = /^(?:万达|万达影城|万达影院)$/u.test(String(existingCinema ?? '').replace(/[（）()\s]/gu, ''));
  const supplement = {
    city,
    cinema: cinemaSupplement && (!existingCinema || genericCinema) ? cinemaSupplement : '',
    movie: movieSupplement(content),
    showtime: timeSupplement(content),
    hall: text(String(content ?? '').match(/第?\s*\d{1,2}\s*号?\s*(?:放映厅|影厅|厅)/u)?.[0].replace(/\s+/gu, ''), 80),
  };
  const source = (name, value) => parsed?.recognition?.[name]
    ? parsed.field_sources?.[name] || 'buyer_text'
    : supplement[name] ? 'buyer_text' : draft.field_sources[name] || '';
  const draftOfficialSelection = draft.recognition.official_selection && typeof draft.recognition.official_selection === 'object'
    && !Array.isArray(draft.recognition.official_selection) ? draft.recognition.official_selection : null;
  const recognition = {
    ...draft.recognition,
    ...(draft.recognition.city || parsed?.recognition?.city || supplement.city ? { city: parsed?.recognition?.city || supplement.city || draft.recognition.city } : {}),
    ...(draft.recognition.cinema || parsed?.recognition?.cinema || supplement.cinema ? { cinema: parsed?.recognition?.cinema || supplement.cinema || draft.recognition.cinema } : {}),
    ...(draft.recognition.movie || parsed?.recognition?.movie || supplement.movie ? { movie: parsed?.recognition?.movie || supplement.movie || draft.recognition.movie } : {}),
    ...(draft.recognition.date || parsed?.recognition?.date ? { date: parsed?.recognition?.date || draft.recognition.date } : {}),
    ...(draft.recognition.showtime || parsed?.recognition?.showtime || supplement.showtime ? { showtime: parsed?.recognition?.showtime || supplement.showtime || draft.recognition.showtime } : {}),
    ...(draft.recognition.hall || parsed?.recognition?.hall || supplement.hall ? { hall: parsed?.recognition?.hall || supplement.hall || draft.recognition.hall } : {}),
    official_selection: draftOfficialSelection?.is_selected === true
      ? draftOfficialSelection
      : parsed?.recognition?.official_selection ?? draftOfficialSelection ?? { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
  };
  const ticketCount = parsed?.ticket_count ?? ticketCountFromText(content) ?? draft.ticket_count;
  const complete = recognition.cinema && recognition.movie && recognition.date && recognition.showtime;
  if (!complete) return parsedText;
  return Object.freeze({
    status: 'recognized', text_quote: true, ticket_count: ticketCount,
    ...(parsed?.semantic_source ? { semantic_source: parsed.semantic_source } : {}),
    recognition: { image_type: 'UNKNOWN', ...recognition },
    field_sources: Object.fromEntries([
      ...['city', 'cinema', 'movie', 'date', 'showtime', 'hall'].map((name) => [name, source(name, recognition[name])]).filter(([, value]) => value),
      ...(Number.isInteger(ticketCount) && ticketCount > 0 ? [['ticket_count', parsed?.ticket_count ? 'buyer_text' : draft.field_sources.ticket_count || 'buyer_text']] : []),
    ]),
  });
}

function fieldSourcesForRecognition(recognition, textFacts, visionRecognition, content = '') {
  const sources = {};
  const supplementHints = { city: cityHintFromSupplement(content), cinema: cinemaHintFromSupplement(content) };
  for (const field of ['city', 'cinema', 'movie', 'date', 'showtime', 'hall']) {
    if (presentText(recognition?.[field])) sources[field] = presentText(visionRecognition?.[field])
      ? 'image'
      : textFacts?.field_sources?.[field] || (supplementHints[field] ? 'buyer_text' : 'image_or_text');
  }
  const ticketCount = Number(textFacts?.ticket_count);
  if (Number.isInteger(ticketCount) && ticketCount > 0) sources.ticket_count = textFacts?.field_sources?.ticket_count || 'buyer_text';
  return sources;
}

function reusableSameImageRecognition(quoteDraft, sourceImageUrl) {
  if (!sourceImageUrl || Number(quoteDraft?.expires_at ?? 0) <= Date.now()
    || text(quoteDraft?.last_image, 2_000) !== sourceImageUrl
    || quoteDraft?.recognition_artifact?.status !== 'recognized') return null;
  const draft = quoteDraftFacts(quoteDraft);
  if (!isQuoteEligibleRecognition(draft.recognition)) return null;
  return Object.freeze({
    status: 'recognized', ticket_count: draft.ticket_count, recognition_reused: 'same_image_quote_draft',
    recognition: Object.freeze(structuredClone(draft.recognition)),
    field_sources: Object.freeze({ ...draft.field_sources }),
  });
}

function repeatedQuoteDraft(quoteDraft, textFacts, sourceImageUrl) {
  const draft = quoteDraftFacts(quoteDraft);
  const lastAttempt = text(quoteDraft?.last_attempt_fingerprint, 2_500);
  const lastImage = text(quoteDraft?.last_image, 2_000);
  if (!lastAttempt || textFacts?.status !== 'recognized' || (sourceImageUrl && sourceImageUrl !== lastImage)) return false;
  const values = ['cinema', 'movie', 'date', 'showtime', 'hall'].map((name) => text(textFacts.recognition?.[name], name === 'hall' ? 80 : 160));
  const ticketCount = Number(textFacts.ticket_count);
  if (values.some((value) => !value) || !Number.isInteger(ticketCount) || ticketCount < 1 || ticketCount > 20) return false;
  const fingerprint = [...values, String(ticketCount), lastImage].join('\u001f');
  return fingerprint === lastAttempt;
}

function canUseTextQuoteAfterIgnoredImage(recognition) {
  const platform = text(recognition?.platform, 40).toUpperCase();
  const cinema = text(recognition?.cinema, 160);
  // An identified non-Wanda screenshot must remain a refusal, and an order
  // confirmation must never be turned into a new quote by nearby text.
  return recognition?.image_type !== 'ORDER_CONFIRM'
    && !['MAOYAN', 'TAOPIAOPIAO'].includes(platform)
    && (!cinema || cinema.includes('万达'));
}

function yuanText(cents) {
  const amount = Number(cents);
  return Number.isSafeInteger(amount) && amount > 0 ? (amount / 100).toFixed(2) : '';
}

function textQuoteReply(quote, recognized) {
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

function isQuoteEligibleRecognition(recognition) {
  const selected = recognition?.official_selection;
  return recognition?.image_type === 'SEAT_MAP'
    || (recognition?.image_type === 'ORDER_CONFIRM'
      && selected?.is_selected === true
      && Array.isArray(selected?.selected_seat_numbers)
      && selected.selected_seat_numbers.length > 0);
}

function mergeImageRecognitions(recognitions) {
  if (!Array.isArray(recognitions) || !recognitions.length) return null;
  let primaryIndex = recognitions.length - 1;
  for (let index = recognitions.length - 1; index >= 0; index -= 1) {
    if (isQuoteEligibleRecognition(recognitions[index])) { primaryIndex = index; break; }
  }
  const merged = { ...recognitions[primaryIndex] };
  for (const [index, support] of recognitions.entries()) {
    if (index === primaryIndex || !support || typeof support !== 'object') continue;
    for (const field of ['city', 'cinema', 'cinema_address_hint', 'movie', 'date', 'showtime', 'hall', 'language_format']) {
      if (!presentText(merged[field]) && presentText(support[field])) merged[field] = support[field];
    }
  }
  return merged;
}

export function createQuotePreviewClient(config, { fetchImpl = globalThis.fetch, imageLoader = null, backendClient = null } = {}) {
  const preview = config?.quotePreview;
  if (!preview) return null;
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');

  const usesTwoStageApi = Boolean(preview.recognizeUrl && preview.quoteUrl);
  const recognitionCache = new Map();
  const recognitionInFlight = new Map();
  const recognitionCacheTtlMs = 5 * 60_000;
  const maximumRecognitionCacheEntries = 100;
  const maximumConcurrentVisionRequests = 4;
  let activeVisionRequests = 0;
  const visionWaiters = [];

  async function withVisionSlot(operation) {
    if (activeVisionRequests >= maximumConcurrentVisionRequests) {
      await new Promise((resolve) => visionWaiters.push(resolve));
    }
    activeVisionRequests += 1;
    try { return await operation(); }
    finally {
      activeVisionRequests -= 1;
      visionWaiters.shift()?.();
    }
  }

  function pruneRecognitionCache(now = Date.now()) {
    for (const [key, entry] of recognitionCache) if (entry.expiresAt <= now) recognitionCache.delete(key);
    while (recognitionCache.size > maximumRecognitionCacheEntries) recognitionCache.delete(recognitionCache.keys().next().value);
  }

  async function recognizeSource(recognitionSource, index, recognitionSources, envelope, payload) {
    const account = text(payload?.accountUnb ?? payload?.account_unb, 128);
    const tenant = text(envelope?.tenantId, 128);
    const message = text(payload?.content ?? payload?.text, 1_000);
    let loadedImage = null;
    let cacheKey = '';
    if (account && imageLoader && backendClient) {
      loadedImage = await imageLoader.load(recognitionSource);
      const imageHash = createHash('sha256').update(loadedImage.bytes).digest('hex');
      const inputHash = createHash('sha256').update(`${message}\u001f${preview.recognizeUrl}`).digest('hex');
      cacheKey = `${tenant}:${account}:${imageHash}:${inputHash}`;
      pruneRecognitionCache();
      const cached = recognitionCache.get(cacheKey);
      if (cached) return { recognition: structuredClone(cached.recognition), reused: true };
      const pending = recognitionInFlight.get(cacheKey);
      if (pending) return { recognition: structuredClone(await pending), reused: true };
    }
    const operation = (async () => {
      const imageUrl = await importImageForRecognition(recognitionSource, envelope, imageLoader, backendClient, loadedImage);
      const response = await withVisionSlot(() => fetchJson(preview.recognizeUrl, {
        event_id: text(recognitionSources.length > 1 ? `${envelope?.id}:image-${index + 1}` : envelope?.id, 128),
        tenant_id: tenant,
        buyer_label: buyerLabel(payload),
        message_text: message,
        image_url: imageUrl,
        ...(ticketCountFromText(payload?.content ?? payload?.text) ? { ticket_count: ticketCountFromText(payload?.content ?? payload?.text) } : {}),
      }, 'quote preview recognition'));
      const item = response?.recognition;
      if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error('quote preview recognition returned an invalid response');
      if (cacheKey) {
        recognitionCache.set(cacheKey, { recognition: structuredClone(item), expiresAt: Date.now() + recognitionCacheTtlMs });
        pruneRecognitionCache();
      }
      return item;
    })();
    if (cacheKey) recognitionInFlight.set(cacheKey, operation);
    try { return { recognition: await operation, reused: false }; }
    finally { if (cacheKey && recognitionInFlight.get(cacheKey) === operation) recognitionInFlight.delete(cacheKey); }
  }

  async function extractSemanticTextFacts(envelope, payload, hasImage) {
    const rawMessage = multilineText(payload?.content ?? payload?.text, 4_000);
    const message = rawMessage.replace(/https?:\/\/[^\s,，。！？、;；]+/giu, ' ').replace(/\s+/gu, ' ').trim();
    if (!preview.textFactUrl || !message) return null;
    try {
      const response = await fetchJson(preview.textFactUrl, {
        event_id: text(envelope?.id, 200) || 'unknown-event', tenant_id: text(envelope?.tenantId, 128) || 'unknown-tenant',
        message_text: message, observed_at: Number.isSafeInteger(Number(envelope?.ts)) && Number(envelope.ts) >= 0 ? Number(envelope.ts) : Date.now(),
      }, 'quote text fact extraction');
      return semanticQuoteFacts(response, { hasImage });
    } catch {
      return null;
    }
  }

  async function recognize(envelope) {
    const payload = envelope?.payload ?? {};
    const sourceImageUrls = recognitionImageUrls(payload);
    const sourceImageUrl = sourceImageUrls.at(-1) ?? firstImageUrl(payload);
    const fallbackText = parseTextQuoteRequest(payload?.content ?? payload?.text, new Date(Number(envelope?.ts) || Date.now()));
    const semanticText = await extractSemanticTextFacts(envelope, payload, Boolean(sourceImageUrl));
    const parsedText = semanticText ?? fallbackText;
    const textFacts = textFactsWithQuoteDraft(parsedText, payload?.quote_draft, payload?.content ?? payload?.text);
    if (repeatedQuoteDraft(payload?.quote_draft, textFacts, sourceImageUrl)) {
      return Object.freeze({ status: 'quote_deduplicated', tenant_id: text(envelope?.tenantId, 128) });
    }
    const reusedRecognition = reusableSameImageRecognition(payload?.quote_draft, sourceImageUrl);
    if (reusedRecognition) {
      const recognition = mergeRecognitionWithTextFacts(reusedRecognition.recognition, textFacts, payload?.content ?? payload?.text);
      return Object.freeze({
        ...reusedRecognition, tenant_id: text(envelope?.tenantId, 128), recognition,
        ticket_count: textFacts?.ticket_count ?? reusedRecognition.ticket_count,
        field_sources: { ...(reusedRecognition.field_sources ?? {}), ...(textFacts?.field_sources ?? {}) },
      });
    }
    if (!sourceImageUrl) {
      return textFacts.status === 'recognized'
        ? Object.freeze({ ...textFacts, tenant_id: text(envelope?.tenantId, 128) })
        : textFacts;
    }
    try {
      const recognitionResults = [];
      const recognitionSources = sourceImageUrls.length ? sourceImageUrls : [sourceImageUrl];
      const outcomes = await Promise.allSettled(recognitionSources.map((recognitionSource, index) => (
        recognizeSource(recognitionSource, index, recognitionSources, envelope, payload)
      )));
      let contentHashReused = false;
      for (const [index, outcome] of outcomes.entries()) {
        if (outcome.status === 'fulfilled') {
          recognitionResults.push(outcome.value.recognition);
          contentHashReused ||= outcome.value.reused;
        }
        // The newest image is the authoritative quote image. An older venue
        // detail is only a bounded identity supplement and may fail safely.
        else if (index === outcomes.length - 1) throw outcome.reason;
      }
      const recognition = mergeImageRecognitions(recognitionResults);
      if (!recognition) throw new Error('quote preview recognition returned no usable image result');
      const hasSelectedConfirmationCard = recognition.image_type === 'ORDER_CONFIRM'
        && recognition?.official_selection?.is_selected === true
        && Array.isArray(recognition?.official_selection?.selected_seat_numbers)
        && recognition.official_selection.selected_seat_numbers.length > 0;
      if (recognition.image_type !== 'SEAT_MAP' && !hasSelectedConfirmationCard) {
        // A buyer may attach a showtime header, chat image, or a second image
        // after sending complete quote facts. Keep that independently
        // verifiable text route available, but never override a recognised
        // non-Wanda cinema or an order-confirmation image.
        if (textFacts.status === 'recognized' && canUseTextQuoteAfterIgnoredImage(recognition)) {
          return Object.freeze({ ...textFacts, tenant_id: text(envelope?.tenantId, 128), vision_fallback: true });
        }
        return Object.freeze({ status: 'ignored', recognition });
      }
      const fusedRecognition = mergeRecognitionWithTextFacts(recognition, textFacts, payload?.content ?? payload?.text);
      return Object.freeze({
        status: 'recognized',
        tenant_id: text(envelope?.tenantId, 128),
        ticket_count: textFacts.ticket_count ?? ticketCountFromText(payload?.content ?? payload?.text),
        recognition: fusedRecognition,
        field_sources: fieldSourcesForRecognition(fusedRecognition, textFacts, recognition, payload?.content ?? payload?.text),
        recognition_reply_text: recognitionReplyText(fusedRecognition),
        ...(textFacts.semantic_source ? { semantic_source: textFacts.semantic_source } : {}),
        ...(contentHashReused ? { recognition_reused: 'same_image_content_hash' } : {}),
      });
    } catch (error) {
      // A complete buyer-supplied text record is independently verifiable by
      // the Wanda matcher. Do not let a transient vision failure block it.
      if (textFacts.status === 'recognized') return Object.freeze({ ...textFacts, tenant_id: text(envelope?.tenantId, 128), vision_fallback: true });
      throw error;
    }
  }

  async function resolveShowtime(recognized) {
    if (recognized?.status !== 'recognized') return recognized;
    const url = preview.quoteUrl.replace(/\/preview-quote(?:\?.*)?$/u, '/preview-resolve-showtime');
    if (url === preview.quoteUrl) throw new Error('showtime resolution endpoint is unavailable');
    const result = await fetchJson(url, { recognition: recognized.recognition }, 'quote preview showtime resolution');
    if (!result?.recognition || typeof result.recognition !== 'object' || Array.isArray(result.recognition)) throw new Error('showtime resolution returned an invalid response');
    return Object.freeze({ ...recognized, status: 'resolved', recognition: result.recognition, matched_cinema_name: text(result.matched_cinema_name, 300) || null });
  }

  async function quote(recognized) {
    if (!['recognized', 'resolved'].includes(recognized?.status)) return recognized;
    let attempt = recognized;
    let outcome = await requestQuote(attempt);
    if (!outcome.response.ok && quoteFailure(outcome.result?.detail, outcome.response.status).code === 'showtime_not_unique') {
      for (const candidate of await resolveMatchCandidates(recognized)) {
        attempt = { ...recognized, recognition: { ...recognized.recognition, ...candidate } };
        outcome = await requestQuote(attempt);
        if (outcome.response.ok) break;
      }
    }
    if (!outcome.response.ok) {
      const failure = quoteFailure(outcome.result?.detail, outcome.response.status);
      const contextualReply = ['showtime_not_found', 'showtime_not_unique', 'cinema_catalog_not_unique', 'official_selection_unverifiable', 'temporary_lock_release_unverified'].includes(failure.code)
        ? quoteFailureReplyText(failure.code, attempt.recognition)
        : '';
      return Object.freeze({ status: 'quote_failed', recognition: attempt.recognition, recognition_reply_text: attempt.recognition_reply_text, failure_code: failure.code, ...(attempt.semantic_source ? { semantic_source: attempt.semantic_source } : {}), ...(failure.diagnostics ? { diagnostics: failure.diagnostics } : {}), reply_text: contextualReply || failure.replyText || quoteFailureReplyText(failure.code, attempt.recognition) });
    }
    const result = outcome.result;
    if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('quote preview quote returned an invalid response');
    const matchedCinema = text(result.matched_cinema_name, 160);
    const recognition = matchedCinema ? { ...attempt.recognition, cinema: matchedCinema } : attempt.recognition;
    if (result.buyer_app_purchase_recommended === true) {
      return Object.freeze({
        status: 'quote_not_competitive',
        recognition,
        recognition_reply_text: recognitionReplyText(recognition),
        reply_text: backendQuoteReplyText(result),
      });
    }
    return Object.freeze({ ...result, status: 'preview_ready', ...(recognized.text_quote ? { text_quote: true } : {}), ...(recognized.semantic_source ? { semantic_source: recognized.semantic_source } : {}), recognition, recognition_reply_text: recognitionReplyText(recognition), reply_text: recognized.text_quote ? textQuoteReply(result, { ...recognized, recognition }) : (backendQuoteReplyText(result) || quoteReplyText(result, recognition)) });
  }

  async function requestQuote(recognized) {
    const response = await fetchImpl(preview.quoteUrl, { method: 'POST', headers: previewHeaders(preview.ingestKey), body: JSON.stringify({ tenant_id: recognized.tenant_id, recognition: recognized.recognition, ...(recognized.ticket_count ? { ticket_count: recognized.ticket_count } : {}) }), signal: AbortSignal.timeout(90_000) });
    return { response, result: await response.json().catch(() => null) };
  }

  async function resolveMatchCandidates(recognized) {
    const url = preview.quoteUrl.replace(/\/preview-quote(?:\?.*)?$/u, '/preview-resolve-candidates');
    if (url === preview.quoteUrl) return [];
    try {
      const response = await fetchImpl(url, { method: 'POST', headers: previewHeaders(preview.ingestKey), body: JSON.stringify({ recognition: recognized.recognition }), signal: AbortSignal.timeout(20_000) });
      const result = await response.json();
      return response.ok && Array.isArray(result?.candidates) ? result.candidates.slice(0, 2).map((candidate) => safeMatchCandidate(candidate, recognized.recognition)).filter(Boolean) : [];
    } catch { return []; }
  }

  async function availableSeats({ recognition, row }) {
    const requestedRow = Number(row);
    if (!recognition || typeof recognition !== 'object' || !Number.isInteger(requestedRow) || requestedRow < 1 || requestedRow > 99) {
      throw new TypeError('valid recognition and row are required');
    }
    const url = preview.quoteUrl.replace(/\/preview-quote(?:\?.*)?$/u, '/preview-available-seats');
    if (url === preview.quoteUrl) throw new Error('available seat endpoint is unavailable');
    const result = await fetchJson(url, { recognition, row: requestedRow }, 'W+ available seat lookup');
    const seats = Array.isArray(result.seats)
      ? result.seats.map((seat) => text(seat, 80)).filter((seat) => new RegExp(`^${requestedRow}排\\d{1,3}座$`, 'u').test(seat)).slice(0, 30)
      : [];
    return Object.freeze({
      row: requestedRow,
      seats,
      available_count: Number.isInteger(result.available_count) && result.available_count >= seats.length ? result.available_count : seats.length,
      wplus_offer_available: result.wplus_offer_available === true,
      matched_cinema_name: text(result.matched_cinema_name, 160) || null,
    });
  }

  async function capture(envelope) {
    if (usesTwoStageApi) return quote(await recognize(envelope));
    const payload = envelope?.payload ?? {};
    const sourceImageUrl = firstImageUrl(payload);
    if (!sourceImageUrl) return { status: 'ignored_no_image' };
    const imageUrl = await importImageForRecognition(sourceImageUrl, envelope, imageLoader, backendClient);
    const result = await fetchJson(preview.ingestUrl, {
      event_id: text(envelope?.id, 128),
      tenant_id: text(envelope?.tenantId, 128),
      buyer_label: buyerLabel(payload),
      message_text: text(payload?.content ?? payload?.text, 1_000),
      image_url: imageUrl,
      ...(ticketCountFromText(payload?.content ?? payload?.text) ? { ticket_count: ticketCountFromText(payload?.content ?? payload?.text) } : {}),
    }, 'quote preview ingestion');
    return Object.freeze({ ...result, reply_text: multilineText(result.reply_text, 500) });
  }

  async function fetchJson(url, body, label) {
    const response = await fetchImpl(url, {
      method: 'POST',
      headers: previewHeaders(preview.ingestKey),
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(90_000),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      const detail = body?.detail;
      const failureCode = typeof detail === 'string' ? detail : typeof detail?.code === 'string' ? detail.code : null;
      const error = new Error(`${label} failed with HTTP ${response.status}`);
      error.failure_code = failureCode;
      error.http_status = response.status;
      error.diagnostics = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail.diagnostics ?? null : null;
      throw error;
    }
    const result = await response.json();
    if (!result || typeof result !== 'object' || Array.isArray(result)) {
      throw new Error(`${label} returned an invalid response`);
    }
    return result;
  }

  return Object.freeze({ recognize, resolveShowtime, quote, availableSeats, capture });
}

function previewHeaders(ingestKey) {
  return {
    accept: 'application/json',
    'content-type': 'application/json',
    'x-wanda-preview-key': ingestKey,
  };
}

function recognitionReplyText(recognition) {
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

function backendQuoteReplyText(quote) {
  return multilineText(quote?.reply_text, 500);
}

function quoteReplyText(quote, recognition = null) {
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

function safeMatchCandidate(candidate, original) {
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return null;
  const result = {};
  for (const field of ['city', 'cinema', 'movie', 'date', 'showtime', 'hall']) {
    const value = text(candidate[field], field === 'date' || field === 'showtime' ? 32 : 160);
    if (value && value !== text(original?.[field], field === 'date' || field === 'showtime' ? 32 : 160)) result[field] = value;
  }
  return Object.keys(result).length ? result : null;
}

function quoteFailure(detail, status) {
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

function quoteFailureReplyText(code, recognition = null) {
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
  if (code === 'temporary_lock_release_unverified') return '临时试价座位的释放状态暂未确认，已停止自动报价；请勿付款，并稍后刷新选座页后重试。';
  if (code === 'wplus_area_unavailable') return '截图位置无法唯一对应当前 W+ 区域，已记录为人工出票位置偏好，请人工确认后处理；不锁座，余票以出票时为准。';
  if (code === 'wplus_price_unavailable') return '该场暂未查到可用的 W+ 会员优惠，请确认是否更换场次或座区。';
  if (code === 'quote_price_conflict') return '当前场次会员优惠不足，按当前规则暂无法形成安全报价，请人工确认。';
  if (code === 'insufficient_available_seats') return '该场当前未查到可用于 W+ 实时核价的座位，可能余票不足或场次已变化；请换场次或刷新官方选座图后重新发送。';
  if (code === 'wanda_gateway_unavailable') return '万达实时核价暂不可用，请稍后重试，无需重复发送全部信息。';
  return '暂未核到该场实时价格，请补充所在城市、完整影院名、影片和开场时间。';
}

