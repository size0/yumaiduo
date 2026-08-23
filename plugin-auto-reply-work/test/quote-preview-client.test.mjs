import assert from 'node:assert/strict';
import test from 'node:test';
import { createQuotePreviewClient, parseTextQuoteRequest } from '../src/quote-preview-client.mjs';

test('quote preview client sends only a masked buyer label and never a reply address', async () => {
  let request;
  const client = createQuotePreviewClient({
    quotePreview: { ingestUrl: 'http://127.0.0.1:8010/api/quotes/preview-ingest', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(url, input) {
      request = { url, ...input };
      return new Response(JSON.stringify({ id: 'preview-1', status: 'UNSENT_PREVIEW' }), { status: 200 });
    },
  });

  const result = await client.capture({
    id: 'event-1',
    tenantId: 'tenant-1',
    payload: {
      buyerNick: '测试买家昵称',
      peerUnb: 'must-not-leave-plugin',
      chatId: 'must-not-leave-plugin',
      content: '  两张 多少钱  ',
      imageUrls: ['https://img.alicdn.com/seat.png'],
    },
  });

  assert.equal(result.status, 'UNSENT_PREVIEW');
  assert.equal(request.url, 'http://127.0.0.1:8010/api/quotes/preview-ingest');
  assert.equal(request.headers['x-wanda-preview-key'], 'a'.repeat(32));
  assert.deepEqual(JSON.parse(request.body), {
    event_id: 'event-1',
    tenant_id: 'tenant-1',
    buyer_label: '测****称',
    message_text: '两张 多少钱',
    image_url: 'https://img.alicdn.com/seat.png',
    ticket_count: 2,
  });
});

test('quote preview client recognizes a seat map before it asks the realtime quote endpoint', async () => {
  const requests = [];
  const client = createQuotePreviewClient({
    quotePreview: {
      recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize',
      quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote',
      ingestKey: 'a'.repeat(32),
    },
  }, {
    async fetchImpl(url, input) {
      requests.push({ url: String(url), body: JSON.parse(input.body) });
      if (String(url).endsWith('/preview-recognize')) {
        return new Response(JSON.stringify({
          recognition: {
            image_type: 'SEAT_MAP', cinema: '北京万达影城通州店', movie: '奥德赛', date: '2026-08-18',
            showtime: '19:30-22:10', hall: '5号厅', official_selection: { selected_seat_numbers: ['7排8座'], selected_count: 1 },
          },
        }), { status: 200 });
      }
      return new Response(JSON.stringify({
        quote_scope: 'exact_seats', seat_zone_type: '普通', member_unit_price_cents: 6190,
        unit_quote_cents: 6290, total_quote_cents: 6290, ticket_count: 1, needs_ticket_count: false,
        pricing_source: 'W+会员专享优惠', detail: 'verified', matched_cinema_name: '北京万达影城通州店',
        reply_text: '※【北京的】| 北京万达影城通州店\n电影：奥德赛\n影厅：5号厅\n场次：2026-08-18 19:30-22:10\n\n62.90元/张，1张合计62.90元。',
      }), { status: 200 });
    },
  });

  const recognized = await client.recognize({
    id: 'event-two-stage', tenantId: 'tenant-1',
    payload: { content: '一张', imageUrls: ['https://img.alicdn.com/seat.png'] },
  });
  assert.equal(recognized.status, 'recognized');
  assert.match(recognized.recognition_reply_text, /北京万达影城通州店/u);
  assert.match(recognized.recognition_reply_text, /7排8座/u);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, 'http://127.0.0.1:8010/api/quotes/preview-recognize');

  const quote = await client.quote(recognized);
  assert.equal(quote.status, 'preview_ready');
  assert.equal(quote.reply_text, '※【北京的】| 北京万达影城通州店\n电影：奥德赛\n影厅：5号厅\n场次：2026-08-18 19:30-22:10\n\n62.90元/张，1张合计62.90元。');
  assert.equal(requests.length, 2);
  assert.equal(requests[1].url, 'http://127.0.0.1:8010/api/quotes/preview-quote');
  assert.deepEqual(requests[1].body, {
    tenant_id: 'tenant-1',
    recognition: recognized.recognition,
    ticket_count: 1,
  });
});

test('quote preview client resolves a showtime through the read-only endpoint before realtime quote', async () => {
  const requests = [];
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(url, input) {
      requests.push({ url: String(url), body: JSON.parse(input.body) });
      return new Response(JSON.stringify({ recognition: { ...JSON.parse(input.body).recognition, cinema: '北京通州万达影城' }, matched_cinema_name: '北京通州万达影城' }), { status: 200 });
    },
  });
  const recognized = { status: 'recognized', tenant_id: 'tenant-1', ticket_count: 2, recognition: { image_type: 'SEAT_MAP', cinema: '通州万达', movie: '测试影片', date: '2026-08-22', showtime: '19:30' } };
  const resolved = await client.resolveShowtime(recognized);
  assert.equal(resolved.status, 'resolved');
  assert.equal(resolved.recognition.cinema, '北京通州万达影城');
  assert.equal(resolved.ticket_count, 2);
  assert.equal(requests[0].url, 'http://127.0.0.1/api/quotes/preview-resolve-showtime');
  assert.deepEqual(requests[0].body, { recognition: recognized.recognition });
});

test('a unit-price question does not override the two seats visible in an official selection card', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl() { return new Response(JSON.stringify({ recognition: {
      image_type: 'ORDER_CONFIRM', cinema: '广州萝岗万达广场店', movie: '去你的岛', date: '2026-08-20', showtime: '21:00', hall: 'VIP厅',
      official_selection: { is_selected: true, selected_seat_numbers: ['4排4座', '4排5座'], selected_count: 2 },
    } }), { status: 200 }); },
  });
  const recognized = await client.recognize({ id: 'unit-price-question', tenantId: 'tenant-1', payload: {
    content: '这个多少钱一张', imageUrls: ['https://img.alicdn.com/selected.png'],
  } });
  assert.equal(recognized.status, 'recognized');
  assert.equal(recognized.ticket_count, null);
  assert.equal(recognized.recognition.official_selection.selected_count, 2);
});

test('two recent screenshots are recognized concurrently and merge identity without mixing seat evidence', async () => {
  const requests = [];
  let activeRequests = 0;
  let maximumActiveRequests = 0;
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(_url, input) {
      const body = JSON.parse(input.body); requests.push(body);
      activeRequests += 1;
      maximumActiveRequests = Math.max(maximumActiveRequests, activeRequests);
      await new Promise((resolve) => setTimeout(resolve, 10));
      activeRequests -= 1;
      if (body.image_url.endsWith('/venue.png')) return new Response(JSON.stringify({ recognition: {
        platform: 'WANDA', image_type: 'OTHER', cinema: '万达影城（南万达广场IMAX店）', cinema_address_hint: '谯城区希夷大道与杜仲路交叉口万达广场',
        movie: '奥德赛', date: '2026-08-20', showtime: '19:30', hall: '1号IMAX厅',
        official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
      } }), { status: 200 });
      return new Response(JSON.stringify({ recognition: {
        platform: 'WANDA', image_type: 'SEAT_MAP', cinema: '万达影城（南万达广场IMAX店）', movie: '奥德赛', date: '2026-08-20', showtime: '19:30-22:22',
        official_selection: { is_selected: true, selected_seat_numbers: ['10排14座', '10排13座'], selected_count: 2 },
      } }), { status: 200 });
    },
  });
  const recognized = await client.recognize({ id: 'multi-image', tenantId: 'tenant-1', payload: {
    imageUrls: ['https://img.alicdn.com/venue.png', 'https://img.alicdn.com/seats.png'],
  } });
  assert.equal(requests.length, 2);
  assert.equal(maximumActiveRequests, 2);
  assert.equal(recognized.status, 'recognized');
  assert.equal(recognized.recognition.image_type, 'SEAT_MAP');
  assert.equal(recognized.recognition.cinema_address_hint, '谯城区希夷大道与杜仲路交叉口万达广场');
  assert.deepEqual(recognized.recognition.official_selection.selected_seat_numbers, ['10排14座', '10排13座']);
});

test('a lower comparable buyer-app price suppresses the merchant quote and cannot authorize an order', async () => {
  const client = createQuotePreviewClient({
    quotePreview: {
      recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize',
      quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote',
      ingestKey: 'a'.repeat(32),
    },
  }, {
    async fetchImpl(url) {
      if (String(url).endsWith('/preview-recognize')) {
        return new Response(JSON.stringify({ recognition: {
          image_type: 'SEAT_MAP', cinema: '北京万达影城通州店', movie: '奥德赛',
          date: '2026-08-18', showtime: '19:30', visible_prices: [{ zone_type: 'W+', price_yuan: 59.9 }],
          confidence: { price: 0.98 },
        } }), { status: 200 });
      }
      return new Response(JSON.stringify({
        quote_scope: 'area_probe', seat_zone_type: 'W+', member_unit_price_cents: 6000,
        unit_quote_cents: 6290, total_quote_cents: 12580, ticket_count: 2, needs_ticket_count: false,
        pricing_source: 'W+会员专享优惠', detail: 'verified', buyer_app_purchase_recommended: true,
        reply_text: '您现在用的APP有合适的优惠价，可以自行购买。',
      }), { status: 200 });
    },
  });

  const recognized = await client.recognize({
    id: 'event-buyer-app-price', tenantId: 'tenant-1',
    payload: { content: '两张多少钱', imageUrls: ['https://img.alicdn.com/seat.png'] },
  });
  const quote = await client.quote(recognized);

  assert.equal(quote.status, 'quote_not_competitive');
  assert.equal(quote.reply_text, '您现在用的APP有合适的优惠价，可以自行购买。');
  assert.equal(quote.unit_quote_cents, undefined);
  assert.equal(quote.total_quote_cents, undefined);
});

test('mixed exact-seat quotes retain each seat price and the authoritative total', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(url) {
      if (String(url).endsWith('/recognize')) return new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', official_selection: { is_selected: true, selected_seat_numbers: ['8排10座', '6排16座'], selected_count: 2 },
      } }), { status: 200 });
      return new Response(JSON.stringify({
        quote_scope: 'exact_seats', seat_zone_type: '未知', member_unit_price_cents: null,
        unit_quote_cents: null, total_quote_cents: 12480, ticket_count: 2, needs_ticket_count: false,
        seat_quotes: [
          { seat_number: '8排10座', seat_zone_type: 'W+', original_price_cents: 8000, member_price_cents: 6190, unit_quote_cents: 6190 },
          { seat_number: '6排16座', seat_zone_type: '普通', original_price_cents: 7000, member_price_cents: 6190, unit_quote_cents: 6290 },
        ], pricing_source: '万达实时座位图 + 后台报价规则', detail: '逐座核验',
      }), { status: 200 });
    },
  });
  const recognized = await client.recognize({ id: 'mixed', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } });
  const quote = await client.quote(recognized);
  assert.equal(quote.total_quote_cents, 12480);
  assert.equal(quote.seat_quotes.length, 2);
  assert.match(quote.reply_text, /8排10座 61\.90元/u);
  assert.match(quote.reply_text, /6排16座 62\.90元/u);
  assert.match(quote.reply_text, /合计124\.80元/u);
});

test('a complete buyer text remains eligible for realtime matching when its attached image is not a seat map', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl() {
      return new Response(JSON.stringify({ recognition: { platform: 'WANDA', image_type: 'OTHER' } }), { status: 200 });
    },
  });

  const recognized = await client.recognize({
    id: 'event-text-with-non-seat-image', tenantId: 'tenant-1', ts: Date.parse('2026-08-18T10:00:00Z'),
    payload: {
      content: '8月19日 十堰万达影城 19:10 欢迎来龙餐馆 7号厅 6排8座 6排9座',
      imageUrls: ['https://img.alicdn.com/showtime-header.png'],
    },
  });

  assert.equal(recognized.status, 'recognized');
  assert.equal(recognized.text_quote, true);
  assert.deepEqual(recognized.recognition, {
    image_type: 'UNKNOWN', city: '十堰', cinema: '十堰万达影城', movie: '欢迎来龙餐馆', date: '2026-08-19', showtime: '19:10', hall: '7号厅',
    official_selection: { is_selected: false, selected_seat_numbers: ['6排8座', '6排9座'], selected_count: 0 },
  });
});

test('a seat-map recognition is completed with explicit buyer text without replacing official selected seats', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl() {
      return new Response(JSON.stringify({ recognition: {
        platform: 'WANDA', image_type: 'SEAT_MAP', cinema: '十堰万达影城',
        official_selection: { is_selected: true, selected_seat_numbers: ['6排8座', '6排9座'], selected_count: 2 },
      } }), { status: 200 });
    },
  });

  const recognized = await client.recognize({
    id: 'event-fuse-image-and-text', tenantId: 'tenant-1', ts: Date.parse('2026-08-18T10:00:00Z'),
    payload: {
      content: '8月19日 十堰万达影城 19:10 欢迎来龙餐馆 7号厅 6排8座 6排9座',
      imageUrls: ['https://img.alicdn.com/seat-map.png'],
    },
  });

  assert.equal(recognized.status, 'recognized');
  assert.equal(recognized.text_quote, undefined);
  assert.deepEqual(recognized.recognition, {
    platform: 'WANDA', image_type: 'SEAT_MAP', cinema: '十堰万达影城',
    official_selection: { is_selected: true, selected_seat_numbers: ['6排8座', '6排9座'], selected_count: 2 },
    city: '十堰', movie: '欢迎来龙餐馆', date: '2026-08-19', showtime: '19:10', hall: '7号厅',
  });
});

test('a verified canonical cinema name replaces the truncated recognition name for downstream quote context', async () => {
  const client = createQuotePreviewClient({ quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) } }, {
    fetchImpl: async (url) => String(url).endsWith('/preview-recognize')
      ? new Response(JSON.stringify({ recognition: { image_type: 'SEAT_MAP', cinema: '北京万达', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }), { status: 200 })
      : new Response(JSON.stringify({ quote_scope: 'area_probe', seat_zone_type: 'WPLUS', member_unit_price_cents: 6190, unit_quote_cents: 6190, total_quote_cents: null, ticket_count: null, needs_ticket_count: true, pricing_source: '万达实时座位图 W+会员价', detail: 'verified', matched_cinema_name: '北京万达影城通州店' }), { status: 200 }),
  });
  const result = await client.capture({ id: 'event-cinema-1', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } });
  assert.equal(result.recognition.cinema, '北京万达影城通州店');
  assert.match(result.reply_text, /^北京万达影城通州店：万达实时座位图与当前规则报价/u);
});

test('an area probe keeps hand-drawn fulfillment diagnostics out of the buyer reply', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    fetchImpl: async (url) => String(url).endsWith('/preview-recognize')
      ? new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', cinema: '诸暨万达广场店',
        hand_drawn_circle: { exists: true, suspected_zone_type: 'W+' },
        official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
      } }), { status: 200 })
      : new Response(JSON.stringify({
        quote_scope: 'area_probe', seat_zone_type: 'W+', member_unit_price_cents: 4470,
        unit_quote_cents: 4900, total_quote_cents: null, ticket_count: null, needs_ticket_count: true,
        pricing_source: '万达实时座位图 + 后台报价规则', detail: 'verified',
      }), { status: 200 }),
  });

  const result = await client.capture({ id: 'event-hand-drawn', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } });

  assert.equal(result.status, 'preview_ready');
  assert.match(result.reply_text, /万达实时座位图与当前规则报价：49.00元\/张/u);
  assert.doesNotMatch(result.reply_text, /手绘圈|人工出票位置偏好|区域实时试价|不锁座/u);
  assert.doesNotMatch(result.reply_text, /W\+会员区实时单价/u);
});

test('available seat lookup calls the read-only row endpoint and filters malformed seat labels', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    fetchImpl: async (url, input) => {
      assert.match(String(url), /preview-available-seats$/u);
      const body = JSON.parse(input.body);
      assert.equal(body.row, 10);
      assert.equal(body.recognition.movie, '奥德赛');
      return new Response(JSON.stringify({ row: 10, seats: ['10排8座', '11排9座', 'bad', '10排10座'], available_count: 4, wplus_offer_available: true, matched_cinema_name: '重庆北碚万达广场店' }), { status: 200 });
    },
  });
  const result = await client.availableSeats({ recognition: { movie: '奥德赛' }, row: 10 });
  assert.deepEqual(result.seats, ['10排8座', '10排10座']);
  assert.equal(result.available_count, 4);
  assert.equal(result.wplus_offer_available, true);
});

test('selected confirmation cards are eligible for realtime quote even if the redundant confirm-button flag is missed', async () => {
  const requests = [];
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(url, input) {
      requests.push({ url: String(url), body: JSON.parse(input.body) });
      if (String(url).endsWith('/preview-recognize')) {
        return new Response(JSON.stringify({ recognition: {
          image_type: 'ORDER_CONFIRM', cinema: '嘉兴万达影城', movie: '奥德赛', date: '2026-08-18', showtime: '19:20-22:12',
          official_selection: { is_selected: true, selected_seat_numbers: ['12排25座', '12排26座'], selected_count: 2 },
          screen_state: { has_confirm_seat_button: false, has_selected_seat_cards: true },
        } }), { status: 200 });
      }
      return new Response(JSON.stringify({
        quote_scope: 'exact_seats', seat_zone_type: '普通', member_unit_price_cents: 6190,
        unit_quote_cents: 6290, total_quote_cents: 12580, ticket_count: 2, needs_ticket_count: false,
        pricing_source: 'W+会员专享优惠', detail: 'verified',
      }), { status: 200 });
    },
  });
  const result = await client.capture({ id: 'event-confirm-seat', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/confirm.png'] } });
  assert.equal(result.status, 'preview_ready');
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1].body.recognition.official_selection.selected_seat_numbers, ['12排25座', '12排26座']);
});

test('cinema eligibility follows the official catalog response instead of a Wanda name substring', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    fetchImpl: async (url) => String(url).endsWith('/preview-recognize')
      ? new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', platform: 'MAOYAN', cinema: '横店电影城（大丰壹方城）', movie: '机器人总动员', showtime: '11:10-12:48',
        official_selection: { is_selected: true, selected_seat_numbers: ['6排10座'], selected_count: 1 },
      } }), { status: 200 })
      : new Response(JSON.stringify({ detail: { code: 'cinema_catalog_not_unique', message: '影院无法在官方影院库唯一匹配', reply_text: '暂未在官方影院库唯一匹配到该影院，请补充所在城市和完整影院名。' } }), { status: 422 }),
  });

  const result = await client.capture({ id: 'event-non-wanda', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } });
  assert.equal(result.failure_code, 'cinema_catalog_not_unique');
  assert.match(result.reply_text, /已识别到横店电影城（大丰壹方城）/u);
  assert.match(result.reply_text, /补充所在城市/u);
  assert.match(result.reply_text, /无需重发截图/u);
  assert.doesNotMatch(result.reply_text, /补充影片|影片名称|开场时间|仅支持万达影城/u);
});

test('quote preview client does not quote a non-seat image and maps a verified quote failure to a precise follow-up', async () => {
  const nonSeatClient = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl() {
      return new Response(JSON.stringify({ recognition: { image_type: 'ORDER_CONFIRM' } }), { status: 200 });
    },
  });
  const ignored = await nonSeatClient.recognize({ id: 'event-order', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/order.png'] } });
  assert.equal(ignored.status, 'ignored');

  const failedQuoteClient = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(url) {
      if (String(url).endsWith('/preview-recognize')) {
        return new Response(JSON.stringify({ recognition: { image_type: 'SEAT_MAP', cinema: '万达影城', official_selection: { selected_count: 0 } } }), { status: 200 });
      }
      return new Response(JSON.stringify({ detail: '未找到可用的 W+ 优惠活动' }), { status: 422 });
    },
  });
  const failed = await failedQuoteClient.quote(await failedQuoteClient.recognize({ id: 'event-wplus', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } }));
  assert.equal(failed.status, 'quote_failed');
  assert.equal(failed.failure_code, 'wplus_price_unavailable');
  assert.match(failed.reply_text, /更换场次或座区/u);
});

test('a bounded model match candidate may retry realtime matching but cannot alter seat selection or price facts', async () => {
  const requests = [];
  const client = createQuotePreviewClient({ quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) } }, {
    async fetchImpl(url, input) {
      const body = JSON.parse(input.body); requests.push({ url: String(url), body });
      if (String(url).endsWith('/preview-recognize')) return new Response(JSON.stringify({ recognition: { image_type: 'SEAT_MAP', cinema: '十堰万达影城', movie: '欢迎来龙餐馆', date: '2026-08-19', showtime: '19:10', official_selection: { is_selected: true, selected_seat_numbers: ['6排8座'], selected_count: 1 } } }), { status: 200 });
      if (String(url).endsWith('/preview-resolve-candidates')) return new Response(JSON.stringify({ candidates: [{ movie: '欢迎来到龙餐馆' }] }), { status: 200 });
      if (body.recognition.movie === '欢迎来龙餐馆') return new Response(JSON.stringify({ detail: { code: 'showtime_not_unique', message: '未匹配到万达场次' } }), { status: 422 });
      return new Response(JSON.stringify({ quote_scope: 'exact_seats', seat_zone_type: 'W+', member_unit_price_cents: 5000, unit_quote_cents: 5000, total_quote_cents: 5000, ticket_count: 1, needs_ticket_count: false, pricing_source: '万达实时座位图 W+会员价', detail: 'verified' }), { status: 200 });
    },
  });
  const quote = await client.capture({ id: 'candidate-retry', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } });
  assert.equal(quote.status, 'preview_ready');
  assert.equal(quote.recognition.movie, '欢迎来到龙餐馆');
  assert.equal(quote.recognition.official_selection.selected_seat_numbers[0], '6排8座');
  assert.equal(requests.filter((request) => request.url.endsWith('/preview-quote')).length, 2);
  assert.deepEqual(requests.find((request) => request.url.endsWith('/preview-resolve-candidates')).body.recognition.official_selection.selected_seat_numbers, ['6排8座']);
});

test('quote failures retain structured safe diagnostics and ask only for the relevant next step', async () => {
  const client = createQuotePreviewClient({ quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) } }, {
    fetchImpl: async (url) => String(url).endsWith('/preview-quote')
      ? new Response(JSON.stringify({ detail: { code: 'insufficient_available_seats', message: '实时座位图中没有足够的同类可用座位', diagnostics: { failure_step: 'select_seats', safe_error_code: 'insufficient_available_seats', requested_match: { city: '北京', cinema: '北京万达影城', movie: '测试影片', date: '2026-08-19', showtime: '20:00', hall: '5号厅', buyer_message: 'must not persist' }, match: { result_count: 1, cinema: '北京万达影城', showtime: '20:00' }, realtime_areas: [{ area_code: 'wplus', sales_price_cents: 8000, wplus_member_price_cents: 6190, available_seat_count: 0 }] } } }), { status: 422 })
      : new Response(JSON.stringify({ recognition: { image_type: 'SEAT_MAP', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }), { status: 200 }),
  });
  const result = await client.capture({ id: 'event-diagnostic-1', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } });
  assert.equal(result.failure_code, 'insufficient_available_seats');
  assert.equal(result.diagnostics.failure_step, 'select_seats');
  assert.deepEqual(result.diagnostics.requested_match, { city: '北京', cinema: '北京万达影城', movie: '测试影片', date: '2026-08-19', showtime: '20:00', hall: '5号厅' });
  assert.equal(result.diagnostics.realtime_areas[0].available_seat_count, 0);
  assert.equal(result.reply_text, '该场当前未查到可用于 W+ 实时核价的座位，可能余票不足或场次已变化；请换场次或刷新官方选座图后重新发送。');
});

test('an unverified W+ area is routed to manual review instead of another price probe', async () => {
  const client = createQuotePreviewClient({ quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) } }, {
    fetchImpl: async (url) => String(url).endsWith('/preview-recognize')
      ? new Response(JSON.stringify({ recognition: { image_type: 'SEAT_MAP', cinema: '余姚万达广场店', hand_drawn_circle: { exists: true }, official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }), { status: 200 })
      : new Response(JSON.stringify({ detail: { code: 'wplus_area_unavailable', message: '实时座位图未找到可核验的 W+区域，请人工复核' } }), { status: 422 }),
  });

  const result = await client.capture({ id: 'unverified-wplus-area', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } });

  assert.equal(result.status, 'quote_failed');
  assert.equal(result.failure_code, 'wplus_area_unavailable');
  assert.match(result.reply_text, /无法唯一对应当前 W\+ 区域/u);
  assert.match(result.reply_text, /人工确认/u);
});

test('a showtime match failure asks only for the unresolved fact instead of repeating known fields', async () => {
  const client = createQuotePreviewClient({ quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) } }, {
    fetchImpl: async (url) => String(url).endsWith('/preview-recognize')
      ? new Response(JSON.stringify({ recognition: { image_type: 'SEAT_MAP', cinema: '十堰万达影城', movie: '欢迎来龙餐馆', date: '2026-08-19', showtime: '19:10', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }), { status: 200 })
      : new Response(JSON.stringify({ detail: { code: 'showtime_not_unique', message: '未匹配到万达场次', reply_text: '截图信息无法唯一匹配场次，请补充影院和开场时间。' } }), { status: 422 }),
  });
  const result = await client.capture({ id: 'known-facts-match-failure', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } });
  assert.match(result.reply_text, /确认影片名称|场次顶部截图/u);
  assert.doesNotMatch(result.reply_text, /完整影院名|开场时间/u);
});

test('zero authoritative showtime matches asks for a refreshed official screenshot instead of repeating known fields', async () => {
  const client = createQuotePreviewClient({ quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) } }, {
    fetchImpl: async (url) => String(url).endsWith('/preview-recognize')
      ? new Response(JSON.stringify({ recognition: { image_type: 'SEAT_MAP', cinema: '宣城万达影城杜比高新店', movie: '奥德赛', date: '2026-08-21', showtime: '22:30', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }), { status: 200 })
      : new Response(JSON.stringify({ detail: { code: 'showtime_not_unique', reply_text: '截图信息无法唯一匹配场次，请补充影院和开场时间。', diagnostics: { requested_match: { cinema: '宣城万达影城杜比高新店', movie: '奥德赛', date: '2026-08-21', showtime: '22:30' }, match: { result_count: 0 } } } }), { status: 422 }),
  });
  const result = await client.capture({ id: 'zero-showtime-match', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } });
  assert.equal(result.failure_code, 'showtime_not_found');
  assert.match(result.reply_text, /未找到该日期和开场时间/u);
  assert.match(result.reply_text, /刷新万达选座页/u);
  assert.doesNotMatch(result.reply_text, /补充影院和开场时间/u);
});

test('zero showtime matches asks for a missing movie before telling the buyer to refresh known cinema and time facts', async () => {
  const client = createQuotePreviewClient({ quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) } }, {
    fetchImpl: async (url) => String(url).endsWith('/preview-recognize')
      ? new Response(JSON.stringify({ recognition: { image_type: 'SEAT_MAP', cinema: '上海临港万达广场店', movie: null, date: '2026-08-23', showtime: '13:20', hall: '1号激光厅', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }), { status: 200 })
      : new Response(JSON.stringify({ detail: { code: 'showtime_not_unique', diagnostics: { requested_match: { cinema: '上海临港万达广场店', movie: '', date: '2026-08-23', showtime: '13:20', hall: '1号激光厅' }, match: { result_count: 0 } } } }), { status: 422 }),
  });
  const result = await client.capture({ id: 'missing-movie-zero-showtime-match', tenantId: 'tenant-1', payload: { imageUrls: ['https://img.alicdn.com/seat.png'] } });
  assert.equal(result.failure_code, 'showtime_not_found');
  assert.match(result.reply_text, /还缺：影片名/u);
  assert.match(result.reply_text, /影片：完整影片名/u);
  assert.doesNotMatch(result.reply_text, /补充影院|刷新万达选座页/u);
});

test('a complete structured text supplement falls back to text-only realtime quote after image recognition fails', async () => {
  const requests = [];
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(url, input) {
      requests.push({ url: String(url), body: JSON.parse(input.body) });
      if (String(url).endsWith('/preview-recognize')) throw new Error('ai_vision_upstream_502');
      return new Response(JSON.stringify({
        quote_scope: 'area_probe', seat_zone_type: 'W+', member_unit_price_cents: 6190,
        unit_quote_cents: 6290, total_quote_cents: 12580, ticket_count: 2, needs_ticket_count: false,
      }), { status: 200 });
    },
  });
  const content = '影院：威海|万达影城（高区九龙汇店）\n影片：欢迎来龙餐馆\n影厅：5号软座沙发厅\n场次：2026-08-19 16:20:00\n座位：5排5座,5排6座\n票数：【2张】';

  const result = await client.capture({
    id: 'event-vision-text-fallback', tenantId: 'tenant-1', ts: Date.parse('2026-08-18T10:00:00Z'),
    payload: { content, imageUrls: ['https://img.alicdn.com/seat.png'] },
  });

  assert.equal(result.status, 'preview_ready');
  assert.equal(requests.length, 2);
  assert.equal(requests[1].body.ticket_count, 2);
  assert.deepEqual(requests[1].body.recognition, {
    image_type: 'UNKNOWN', cinema: '威海|万达影城（高区九龙汇店）', movie: '欢迎来龙餐馆', date: '2026-08-19', showtime: '16:20', hall: '5号软座沙发厅',
    official_selection: { is_selected: false, selected_seat_numbers: ['5排5座', '5排6座'], selected_count: 0 },
  });
});

test('a short text supplement deterministically fuses the active quote draft with VLM facts', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    fetchImpl: async (url) => String(url).endsWith('/preview-recognize')
      ? new Response(JSON.stringify({ recognition: { image_type: 'SEAT_MAP', cinema: '十堰万达影城', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 } } }), { status: 200 })
      : new Response(JSON.stringify({ detail: 'not used' }), { status: 422 }),
  });

  const result = await client.recognize({
    id: 'draft-supplement', tenantId: 'tenant-1',
    payload: {
      content: '19:10这场', imageUrls: ['https://img.alicdn.com/seat.png'],
      quote_draft: {
        fields: {
          cinema: { value: '十堰万达影城', source: 'buyer_text', confidence: 0.96 },
          movie: { value: '欢迎来龙餐馆', source: 'buyer_text', confidence: 0.82 },
          date: { value: '2026-08-19', source: 'buyer_text', confidence: 1 },
          hall: { value: '7号厅', source: 'buyer_text', confidence: 0.95 },
          ticket_count: { value: 2, source: 'typed_seats', confidence: 0.9 },
        },
        last_image: 'https://img.alicdn.com/seat.png', state: 'collecting', expires_at: Date.now() + 60_000,
      },
    },
  });

  assert.equal(result.status, 'recognized');
  assert.deepEqual(result.recognition, {
    image_type: 'SEAT_MAP', cinema: '十堰万达影城', movie: '欢迎来龙餐馆', date: '2026-08-19', showtime: '19:10', hall: '7号厅',
    official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
  });
  assert.equal(result.ticket_count, 2);
  assert.deepEqual(result.field_sources, {
    cinema: 'image', movie: 'buyer_text', date: 'buyer_text', showtime: 'buyer_text', hall: 'buyer_text', ticket_count: 'typed_seats',
  });
});

test('AI semantic facts are the primary natural-language input while image evidence remains authoritative', async () => {
  const calls = [];
  const client = createQuotePreviewClient({
    quotePreview: {
      recognizeUrl: 'http://127.0.0.1/preview-recognize', textFactUrl: 'http://127.0.0.1/preview-extract-text',
      quoteUrl: 'http://127.0.0.1/preview-quote', ingestKey: 'a'.repeat(32),
    },
  }, {
    async fetchImpl(url, input) {
      calls.push(String(url));
      if (String(url).endsWith('/preview-extract-text')) {
        const body = JSON.parse(input.body);
        assert.match(body.message_text, /济南世贸万达影城/u);
        assert.doesNotMatch(body.message_text, /https?:\/\//u);
        return new Response(JSON.stringify({ status: 'extracted', extractor_version: 'wanda-quote-fact-extractor-v1', facts: {
          quote_intent: true, city: '济南', cinema: '济南世贸万达影城', movie: '奥德赛', date: '2026-08-23', showtime: '12:35', hall: null,
          ticket_count: 2, seat_numbers: [], requested_row: null, refers_to_image_positions: true, confidence: 0.99,
        } }), { status: 200 });
      }
      return new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', platform: 'MAOYAN', cinema: '万达影城（世茂杜比影院店）', movie: '奥德赛',
        date: '2026-08-23', showtime: '12:35-15:27', hall: '8号杜比全景声激光巨幕厅',
        official_selection: { is_selected: true, selected_seat_numbers: ['9排14座', '9排15座'], selected_count: 2 },
      } }), { status: 200 });
    },
  });
  const result = await client.recognize({
    id: 'ai-semantic-jinan', tenantId: '107', ts: Date.parse('2026-08-23T02:31:18.221Z'),
    payload: {
      content: 'https://img.alicdn.com/seat.jpg\n您好 请问济南世贸万达影城今日12:35开场的奥德赛这两个位置还有票吗？',
      imageUrls: ['https://img.alicdn.com/seat.jpg'],
    },
  });

  assert.deepEqual(calls.sort(), ['http://127.0.0.1/preview-extract-text', 'http://127.0.0.1/preview-recognize']);
  assert.equal(result.status, 'recognized');
  assert.equal(result.recognition.city, '济南');
  assert.equal(result.recognition.cinema, '万达影城（世茂杜比影院店）');
  assert.equal(result.recognition.hall, '8号杜比全景声激光巨幕厅');
  assert.equal(result.ticket_count, 2);
  assert.equal(result.field_sources.city, 'ai_text');
  assert.equal(result.semantic_source, 'wanda-quote-fact-extractor-v1');
  assert.deepEqual(result.recognition.official_selection.selected_seat_numbers, ['9排14座', '9排15座']);
});

test('an image keeps the explicit city from the preceding partial text draft', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl() {
      return new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', platform: 'MAOYAN', cinema: '万达影城（世茂杜比影院店）', movie: '奥德赛',
        date: '2026-08-23', showtime: '12:35-15:27', hall: '8号厅',
        official_selection: { is_selected: true, selected_seat_numbers: ['9排14座', '9排15座'], selected_count: 2 },
      } }), { status: 200 });
    },
  });
  const result = await client.recognize({
    id: 'jinan-context-image', tenantId: 'tenant-1', payload: {
      content: '您好 请问济南世贸万达影城今日12:35开场的奥德赛这两个位置还有票吗？',
      imageUrls: ['https://img.alicdn.com/seat.png'],
      quote_draft: { fields: {
        city: { value: '济南', source: 'buyer_text', confidence: 1 },
        cinema: { value: '济南世贸万达影城', source: 'buyer_text', confidence: 0.9 },
        movie: { value: '奥德赛', source: 'buyer_text', confidence: 1 },
        date: { value: '2026-08-23', source: 'buyer_text', confidence: 1 },
        showtime: { value: '12:35', source: 'buyer_text', confidence: 1 },
        ticket_count: { value: 2, source: 'buyer_text', confidence: 0.9 },
      }, expires_at: Date.now() + 60_000 },
    },
  });

  assert.equal(result.status, 'recognized');
  assert.equal(result.recognition.city, '济南');
  assert.equal(result.recognition.cinema, '万达影城（世茂杜比影院店）');
  assert.equal(result.recognition.movie, '奥德赛');
  assert.equal(result.ticket_count, 2);
  assert.deepEqual(result.recognition.official_selection.selected_seat_numbers, ['9排14座', '9排15座']);
});

test('an explicit movie-name reply completes an active image quote draft without another vision request', async () => {
  let requests = 0;
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl() { requests += 1; throw new Error('vision must not run for a text movie supplement'); },
  });
  const recognized = await client.recognize({
    id: 'movie-supplement', tenantId: 'tenant-1', payload: {
      content: '影片：捕风追影', imageUrls: [],
      quote_draft: {
        fields: {
          cinema: { value: '上海临港万达广场店', source: 'image', confidence: 0.9 },
          date: { value: '2026-08-23', source: 'image', confidence: 0.9 },
          showtime: { value: '13:20', source: 'image', confidence: 0.9 },
          hall: { value: '1号激光厅', source: 'image', confidence: 0.9 },
          ticket_count: { value: 1, source: 'typed_seats', confidence: 0.9 },
        },
        recognition_artifact: { recognition: {
          platform: 'WANDA', image_type: 'SEAT_MAP', cinema: '上海临港万达广场店', date: '2026-08-23',
          showtime: '13:20', hall: '1号激光厅', official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
        } },
        state: 'matching', expires_at: Date.now() + 60_000,
      },
    },
  });
  assert.equal(requests, 0);
  assert.equal(recognized.status, 'recognized');
  assert.equal(recognized.recognition.movie, '捕风追影');
  assert.equal(recognized.ticket_count, 1);
  assert.equal(recognized.field_sources.movie, 'buyer_text');
});

test('a short city supplement augments the image draft without changing seats or price facts', async () => {
  let quoteBody = null;
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(url, input) {
      if (String(url).endsWith('/recognize')) return new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', cinema: '万达影城（中都荟店）',
        official_selection: { is_selected: true, selected_seat_numbers: ['8排10座'], selected_count: 1 },
      } }), { status: 200 });
      quoteBody = JSON.parse(input.body);
      return new Response(JSON.stringify({ quote_scope: 'exact_seats', unit_quote_cents: 6000, total_quote_cents: 6000, ticket_count: 1, needs_ticket_count: false, seat_quotes: [], pricing_source: '实时', detail: 'ok' }), { status: 200 });
    },
  });
  const recognized = await client.recognize({
    id: 'city-supplement', tenantId: 'tenant-1', payload: {
      content: '广州', imageUrls: ['https://img.alicdn.com/seat.png'],
      quote_draft: { fields: {
        cinema: { value: '万达影城（中都荟店）', source: 'image', confidence: 0.9 },
        movie: { value: '测试影片', source: 'image', confidence: 0.9 },
        date: { value: '2026-08-19', source: 'image', confidence: 0.9 },
        showtime: { value: '19:10', source: 'image', confidence: 0.9 },
      }, expires_at: Date.now() + 60_000 },
    },
  });
  await client.quote(recognized);
  assert.equal(quoteBody.recognition.city, '广州');
  assert.equal(quoteBody.recognition.cinema, '万达影城（中都荟店）');
  assert.deepEqual(quoteBody.recognition.official_selection.selected_seat_numbers, ['8排10座']);
});

test('a natural branch price question completes a draft and preserves official selected seats', async () => {
  let quoteBody = null;
  let requests = 0;
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl(_url, input) {
      requests += 1;
      quoteBody = JSON.parse(input.body);
      return new Response(JSON.stringify({
        quote_scope: 'exact_seats', seat_zone_type: 'W+', member_unit_price_cents: 6000,
        unit_quote_cents: 6200, total_quote_cents: 12400, ticket_count: 2,
        needs_ticket_count: false, seat_quotes: [], pricing_source: '实时', detail: 'ok',
        matched_cinema_name: '宁波奉化万达广场店',
      }), { status: 200 });
    },
  });
  const recognized = await client.recognize({
    id: 'ningbo-fenghua-supplement', tenantId: 'tenant-1', payload: {
      content: '宁波奉化万达多少', imageUrls: [],
      quote_draft: {
        fields: {
          movie: { value: '奥德赛', source: 'image', confidence: 0.9 },
          date: { value: '2026-08-22', source: 'image', confidence: 0.9 },
          showtime: { value: '19:30-22:23', source: 'image', confidence: 0.9 },
          hall: { value: '7号IMAX厅', source: 'image', confidence: 0.9 },
        },
        recognition_artifact: { recognition: {
          platform: 'WANDA', image_type: 'SEAT_MAP', movie: '奥德赛', date: '2026-08-22',
          showtime: '19:30-22:23', hall: '7号IMAX厅',
          official_selection: {
            is_selected: true, selected_seat_numbers: ['9排22座', '9排23座'], selected_count: 2,
            seats: [{ seat_number: '9排22座', price: 73.9 }, { seat_number: '9排23座', price: 73.9 }],
            total_price: 147.8,
          },
        } },
        last_image: 'https://img.alicdn.com/seat.png', state: 'collecting', expires_at: Date.now() + 60_000,
      },
    },
  });

  assert.equal(requests, 0);
  assert.equal(recognized.status, 'recognized');
  assert.equal(recognized.recognition.city, '宁波');
  assert.equal(recognized.recognition.cinema, '宁波奉化万达');
  assert.deepEqual(recognized.recognition.official_selection.selected_seat_numbers, ['9排22座', '9排23座']);
  assert.equal(recognized.ticket_count, 2);

  const quoted = await client.quote(recognized);
  assert.equal(requests, 1);
  assert.equal(quoted.status, 'preview_ready');
  assert.equal(quoteBody.recognition.cinema, '宁波奉化万达');
  assert.deepEqual(quoteBody.recognition.official_selection.selected_seat_numbers, ['9排22座', '9排23座']);
  assert.equal(quoteBody.ticket_count, 2);
});

test('province-city supplement fills the image city deterministically', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl() {
      return new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', cinema: '万达影城（IMAX广场店）', movie: '大唐妖探',
        date: '2026-08-22', showtime: '17:50', hall: '8号厅-激光厅',
        official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
      } }), { status: 200 });
    },
  });

  const result = await client.recognize({
    id: 'province-city-supplement', tenantId: 'tenant-1', payload: {
      content: '山东德州', imageUrls: ['https://img.alicdn.com/seat.png'],
    },
  });

  assert.equal(result.status, 'recognized');
  assert.equal(result.recognition.city, '德州');
  assert.equal(result.field_sources.city, 'buyer_text');
});

test('administrative city and complete Wanda cinema supplements fill missing image recognition facts', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl() {
      return new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', movie: '奥德赛', date: '2026-08-20', showtime: '19:30', hall: '2号厅',
        official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
      } }), { status: 200 });
    },
  });

  const result = await client.recognize({
    id: 'administrative-location-supplement', tenantId: 'tenant-1', payload: {
      content: '内蒙古自治区巴彦淖尔市临河区\n万达影城 PRIME（摩尔城店）',
      imageUrls: ['https://img.alicdn.com/seat.png'],
    },
  });

  assert.equal(result.status, 'recognized');
  assert.equal(result.recognition.city, '巴彦淖尔');
  assert.equal(result.recognition.cinema, '万达影城 PRIME（摩尔城店）');
  assert.equal(result.field_sources.city, 'buyer_text');
  assert.equal(result.field_sources.cinema, 'buyer_text');
});

test('a natural Shandong Zibo branch sentence fills city and cinema omitted by the screenshot', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl() {
      return new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', movie: '奥德赛', date: '2026-08-22', showtime: '19:00',
        official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
      } }), { status: 200 });
    },
  });
  const result = await client.recognize({
    id: 'zibo-natural-location', tenantId: 'tenant-1', payload: {
      content: '你好，问一下山东省淄博市张店区富力万达的万达影城，今天晚上7点的奥德赛，位置7排15，16和8排16的价格',
      imageUrls: ['https://img.alicdn.com/seat.png'],
    },
  });
  assert.equal(result.status, 'recognized');
  assert.equal(result.recognition.city, '淄博');
  assert.equal(result.recognition.cinema, '富力万达');
  assert.equal(result.field_sources.city, 'buyer_text');
  assert.equal(result.field_sources.cinema, 'buyer_text');
});

test('a distinctive cinema branch keyword fills a missing image cinema without inventing other facts', async () => {
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    async fetchImpl() {
      return new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', movie: '奥德赛', date: '2026-08-20', showtime: '16:15', hall: 'IMAX激光厅',
        official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
      } }), { status: 200 });
    },
  });

  const result = await client.recognize({ id: 'branch-keyword', tenantId: 'tenant-1', payload: { content: '浦东陆悦天地', imageUrls: ['https://img.alicdn.com/seat.png'] } });
  assert.equal(result.status, 'recognized');
  assert.equal(result.recognition.cinema, '浦东陆悦天地');
  assert.equal(result.field_sources.cinema, 'buyer_text');
});

test('an unchanged attempted quote draft skips the repeated vision request', async () => {
  let calls = 0;
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    fetchImpl: async () => { calls += 1; throw new Error('the duplicate event must not reach vision'); },
  });
  const values = ['\u5341\u5830\u4e07\u8fbe\u5f71\u57ce', '\u6d4b\u8bd5\u7535\u5f71', '2026-08-19', '19:10', '7\u53f7\u5385', '2'];
  const result = await client.recognize({
    id: 'duplicate-draft', tenantId: 'tenant-1',
    payload: {
      content: '19:10', imageUrls: ['https://img.alicdn.com/seat.png'],
      quote_draft: {
        fields: {
          cinema: { value: values[0], source: 'buyer_text', confidence: 0.96 }, movie: { value: values[1], source: 'buyer_text', confidence: 0.82 },
          date: { value: values[2], source: 'buyer_text', confidence: 1 }, showtime: { value: values[3], source: 'buyer_text', confidence: 1 },
          hall: { value: values[4], source: 'buyer_text', confidence: 0.95 }, ticket_count: { value: 2, source: 'typed_seats', confidence: 0.9 },
        },
        last_image: 'https://img.alicdn.com/seat.png', last_attempt_fingerprint: [...values, 'https://img.alicdn.com/seat.png'].join('\u001f'), expires_at: Date.now() + 60_000,
      },
    },
  });

  assert.equal(result.status, 'quote_deduplicated');
  assert.equal(calls, 0);
});

test('an identical image reuses its unexpired recognition artifact but still allows a fresh realtime quote', async () => {
  let calls = 0;
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize', quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote', ingestKey: 'a'.repeat(32) },
  }, {
    fetchImpl: async () => { calls += 1; throw new Error('the identical image must not reach vision again'); },
  });
  const artifact = {
    status: 'recognized', tenant_id: 'tenant-1', ticket_count: 2,
    recognition: {
      image_type: 'ORDER_CONFIRM', cinema: '赣州万达影城远洋未来广场店', movie: '蜘蛛侠：崭新之日',
      date: '2026-08-22', showtime: '20:00', hall: '1号厅',
      official_selection: { is_selected: true, selected_seat_numbers: ['4排4座', '4排3座'], selected_count: 2 },
    },
    field_sources: { cinema: 'image', movie: 'image', date: 'image', showtime: 'image', hall: 'image', ticket_count: 'image' },
  };
  const result = await client.recognize({
    id: 'same-image-retry', tenantId: 'tenant-1',
    payload: {
      imageUrls: ['https://img.alicdn.com/same-seat.png'],
      quote_draft: { recognition_artifact: artifact, last_image: 'https://img.alicdn.com/same-seat.png', expires_at: Date.now() + 60_000 },
    },
  });
  assert.equal(calls, 0);
  assert.equal(result.status, 'recognized');
  assert.equal(result.recognition_reused, 'same_image_quote_draft');
  assert.equal(result.recognition.official_selection.selected_count, 2);
  assert.deepEqual(result.recognition.official_selection.selected_seat_numbers, ['4排4座', '4排3座']);
});

test('same image bytes under different URLs reuse recognition only within the same account', async () => {
  let uploads = 0;
  let recognitions = 0;
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    imageLoader: { async load() { return { bytes: new Uint8Array([1, 2, 3, 4]), contentType: 'image/png' }; } },
    backendClient: { async uploadTestImage() { uploads += 1; return { url: `https://bucket.example/import-${uploads}.png` }; } },
    fetchImpl: async () => {
      recognitions += 1;
      return new Response(JSON.stringify({ recognition: {
        image_type: 'ORDER_CONFIRM', cinema: '测试万达影城', movie: '测试影片', date: '2026-08-22', showtime: '20:00',
        official_selection: { is_selected: true, selected_seat_numbers: ['4排3座', '4排4座'], selected_count: 2 },
      } }), { status: 200 });
    },
  });
  const request = (id, accountUnb, imageUrl) => client.recognize({
    id, tenantId: 'tenant-1', payload: { accountUnb, imageUrls: [imageUrl] },
  });
  const first = await request('content-1', 'shop-1', 'https://img.alicdn.com/first.png');
  const second = await request('content-2', 'shop-1', 'https://img.alicdn.com/renamed.png');
  const isolated = await request('content-3', 'shop-2', 'https://img.alicdn.com/other-account.png');
  assert.equal(first.status, 'recognized');
  assert.equal(second.recognition_reused, 'same_image_content_hash');
  assert.equal(isolated.recognition_reused, undefined);
  assert.equal(recognitions, 2);
  assert.equal(uploads, 2);
});

test('vision requests use a bounded global concurrency limit', async () => {
  let active = 0;
  let maximum = 0;
  const client = createQuotePreviewClient({
    quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
  }, {
    imageLoader: { async load(url) { return { bytes: new TextEncoder().encode(url), contentType: 'image/png' }; } },
    backendClient: { async uploadTestImage(_image, context) { return { url: `https://bucket.example/${context.eventId}.png` }; } },
    fetchImpl: async () => {
      active += 1;
      maximum = Math.max(maximum, active);
      await new Promise((resolve) => setTimeout(resolve, 15));
      active -= 1;
      return new Response(JSON.stringify({ recognition: {
        image_type: 'SEAT_MAP', cinema: '测试万达影城', movie: '测试影片', date: '2026-08-22', showtime: '20:00',
        official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
      } }), { status: 200 });
    },
  });
  await Promise.all(Array.from({ length: 6 }, (_, index) => client.recognize({
    id: `bounded-${index}`, tenantId: 'tenant-1', payload: { accountUnb: 'shop-1', imageUrls: [`https://img.alicdn.com/${index}.png`] },
  })));
  assert.equal(maximum, 4);
});

test('structured seat and release failures override a generic backend reply with precise safe guidance', async () => {
  for (const scenario of [
    {
      code: 'official_selection_unverifiable',
      expected: /官方已选座当前并非全部实时可选.*重新选择/u,
      rejected: /补充.*影院名|已转人工/u,
    },
    {
      code: 'temporary_lock_release_unverified',
      expected: /尚未在万达实时座位图中确认恢复.*不代表该场会员座都不可售.*请勿付款/u,
      rejected: /补充.*影院名|已转人工/u,
    },
  ]) {
    const client = createQuotePreviewClient({
      quotePreview: { recognizeUrl: 'http://127.0.0.1/recognize', quoteUrl: 'http://127.0.0.1/quote', ingestKey: 'a'.repeat(32) },
    }, {
      fetchImpl: async () => new Response(JSON.stringify({ detail: {
        code: scenario.code, reply_text: '暂未核到该场实时价格，请补充完整影院名、影片和开场时间。',
      } }), { status: scenario.code === 'temporary_lock_release_unverified' ? 502 : 422 }),
    });
    const result = await client.quote({
      status: 'recognized', ticket_count: 2,
      recognition: { image_type: 'ORDER_CONFIRM', cinema: '测试万达影城', movie: '测试影片', date: '2026-08-22', showtime: '20:00', official_selection: { is_selected: true, selected_seat_numbers: ['4排3座', '4排4座'], selected_count: 2 } },
    });
    assert.equal(result.failure_code, scenario.code);
    assert.match(result.reply_text, scenario.expected);
    assert.doesNotMatch(result.reply_text, scenario.rejected);
  }
});

test('quote preview client uploads the verified inbound image to COS before requesting recognition', async () => {
  let request;
  let uploaded;
  const client = createQuotePreviewClient({
    quotePreview: { ingestUrl: 'http://127.0.0.1:8010/api/quotes/preview-ingest', ingestKey: 'a'.repeat(32) },
  }, {
    imageLoader: {
      async load(url) {
        assert.equal(url, 'https://img.alicdn.com/seat.png');
        return { bytes: new Uint8Array([0x89, 0x50, 0x4e, 0x47]), contentType: 'image/png' };
      },
    },
    backendClient: {
      async uploadTestImage(image, context) {
        uploaded = { image, context };
        return { url: 'https://bucket.example/wanda-vision/inbound.png' };
      },
    },
    fetchImpl: async (_url, init) => {
      request = JSON.parse(init.body);
      return new Response(JSON.stringify({ status: 'preview_ready' }), { status: 200 });
    },
  });

  await client.capture({
    id: 'event-cos-import',
    tenantId: 'tenant-1',
    payload: { imageUrls: ['https://img.alicdn.com/seat.png'] },
  });

  assert.deepEqual([...uploaded.image.bytes], [0x89, 0x50, 0x4e, 0x47]);
  assert.equal(uploaded.image.contentType, 'image/png');
  assert.deepEqual(uploaded.context, { tenantId: 'tenant-1', eventId: 'event-cos-import' });
  assert.equal(request.image_url, 'https://bucket.example/wanda-vision/inbound.png');
});

test('quote preview client accepts an image URL delivered as message text', async () => {
  let request;
  const client = createQuotePreviewClient({
    quotePreview: { ingestUrl: 'http://127.0.0.1:8010/api/quotes/preview-ingest', ingestKey: 'a'.repeat(32) },
  }, {
    fetchImpl: async (_url, init) => {
      request = JSON.parse(init.body);
      return new Response(JSON.stringify({ status: 'preview_ready' }), { status: 200 });
    },
  });

  await client.capture({
    id: 'event-image-text',
    payload: { content: 'https://img.alicdn.com/example/seat-map.jpg' },
  });

  assert.equal(request.image_url, 'https://img.alicdn.com/example/seat-map.jpg');
  assert.equal(request.tenant_id, '');
});

test('structured buyer text is parsed into a non-exact realtime quote request', async () => {
  const parsed = parseTextQuoteRequest('8月22日 上海闵行颛桥万达 16:25 奥德赛 7排14座 7排15座', new Date('2026-08-18T10:00:00Z'));
  assert.equal(parsed.status, 'recognized');
  assert.equal(parsed.text_quote, true);
  assert.equal(parsed.ticket_count, 2);
  assert.deepEqual(parsed.recognition, {
    image_type: 'UNKNOWN', city: '上海', cinema: '上海闵行颛桥万达', movie: '奥德赛', date: '2026-08-22', showtime: '16:25',
    official_selection: { is_selected: false, selected_seat_numbers: ['7排14座', '7排15座'], selected_count: 0 },
  });
});

test('a compact one-line showtime strips the inline hall from the movie while retaining it as a fact', () => {
  const parsed = parseTextQuoteRequest('8月19日 十堰万达影城 19:10欢迎来龙餐馆 7号厅6排8座 6排9座', new Date('2026-08-18T10:00:00Z'));

  assert.equal(parsed.status, 'recognized');
  assert.equal(parsed.ticket_count, 2);
  assert.deepEqual(parsed.recognition, {
    image_type: 'UNKNOWN', city: '十堰', cinema: '十堰万达影城', movie: '欢迎来龙餐馆', date: '2026-08-19', showtime: '19:10', hall: '7号厅',
    official_selection: { is_selected: false, selected_seat_numbers: ['6排8座', '6排9座'], selected_count: 0 },
  });
});

test('an explicit bare count beside a seat-position phrase supplies a non-official ticket count', () => {
  const parsed = parseTextQuoteRequest('2026年8月20日 成都崇州万达广场店 14:30 欢迎来龙餐馆 8排中间两个');
  assert.equal(parsed.status, 'recognized');
  assert.equal(parsed.ticket_count, 2);
});

test('Chinese row numerals and space-separated seat numbers supply a non-official three-ticket count', () => {
  const parsed = parseTextQuoteRequest('8月19日 黄冈万达广场 16:30 欢迎来龙餐馆 八排12 13 14', new Date('2026-08-18T10:00:00Z'));

  assert.equal(parsed.status, 'recognized');
  assert.equal(parsed.ticket_count, 3);
  assert.deepEqual(parsed.recognition.official_selection, {
    is_selected: false, selected_seat_numbers: ['8排12座', '8排13座', '8排14座'], selected_count: 0,
  });
});

test('a typed seat range supplies count and context without becoming an official selection', () => {
  const parsed = parseTextQuoteRequest('8月20日 常州新北万达广场店 19:20 奥德赛 9排18-19', new Date('2026-08-18T10:00:00Z'));

  assert.equal(parsed.status, 'recognized');
  assert.equal(parsed.ticket_count, 2);
  assert.deepEqual(parsed.recognition.official_selection, {
    is_selected: false, selected_seat_numbers: ['9排18座', '9排19座'], selected_count: 0,
  });
});

test('compact province-city cinema date movie time-range and typed seat are parsed', async () => {
  const result = parseTextQuoteRequest('河南郑州  中原万达 9.23奥德赛9:30-12:22  9排18座', new Date('2026-08-20T12:00:00+08:00'));
  assert.equal(result.status, 'recognized');
  assert.equal(result.recognition.city, '郑州');
  assert.equal(result.recognition.cinema, '中原万达');
  assert.equal(result.recognition.movie, '奥德赛');
  assert.equal(result.recognition.date, '2026-09-23');
  assert.equal(result.recognition.showtime, '09:30');
  assert.equal(result.ticket_count, 1);
  assert.deepEqual(result.recognition.official_selection, {
    is_selected: false,
    selected_seat_numbers: ['9排18座'],
    selected_count: 0,
  });
});


test('a natural today request preserves explicit city and identity while requiring the referenced-seat image', () => {
  const result = parseTextQuoteRequest(
    '您好 请问济南世贸万达影城今日12:35开场的奥德赛这两个位置还有票吗？',
    new Date('2026-08-23T02:31:18.221Z'),
  );
  assert.equal(result.status, 'needs_confirmation');
  assert.equal(result.failure_code, 'text_quote_missing_fields');
  assert.deepEqual(result.missing_fields, ['这几个位置的完整选座截图']);
  assert.equal(result.ticket_count, 2);
  assert.deepEqual(result.recognition, {
    image_type: 'UNKNOWN', city: '济南', cinema: '济南世贸万达影城', movie: '奥德赛',
    date: '2026-08-23', showtime: '12:35',
    official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
  });
});

test('natural Chinese date, night time, and format suffixes are parsed for a text quote', async () => {
  const parsed = parseTextQuoteRequest('西安大明宫万达，8.20日晚上10.40那场奥德赛imax多少钱一张老板', new Date('2026-08-18T10:00:00Z'));
  assert.equal(parsed.status, 'recognized');
  assert.equal(parsed.ticket_count, null);
  assert.deepEqual(parsed.recognition, {
    image_type: 'UNKNOWN', city: '西安', cinema: '西安大明宫万达', movie: '奥德赛', date: '2026-08-20', showtime: '22:40',
    official_selection: { is_selected: false, selected_seat_numbers: [], selected_count: 0 },
  });
});

test('weekday date and compact seat lists are understood but an approximate time requires clarification', async () => {
  const parsed = parseTextQuoteRequest('周六上午9点多 鞍山万达影城（高新区） 欢迎来龙餐馆 7排5.6.7号 多少钱一张票', new Date('2026-08-18T10:00:00Z'));
  assert.equal(parsed.status, 'needs_confirmation');
  assert.match(parsed.reply_text, /准确开场时间/u);
  assert.doesNotMatch(parsed.reply_text, /还缺：[^。]*日期/u);
  assert.doesNotMatch(parsed.reply_text, /还缺：[^。]*完整万达影院名/u);
  assert.doesNotMatch(parsed.reply_text, /还缺：[^。]*影片名/u);
});

test('multi-line text accepts the ratio colon, cinema plaza name, and inherited compact row seats', async () => {
  const parsed = parseTextQuoteRequest('本周六 8月22日\n09∶55\n欢迎来龙餐馆\n鞍山高新区万达广场\n7排6座、7座、8座', new Date('2026-08-18T10:00:00Z'));
  assert.equal(parsed.status, 'recognized');
  assert.equal(parsed.ticket_count, 3);
  assert.deepEqual(parsed.recognition, {
    image_type: 'UNKNOWN', cinema: '鞍山高新区万达广场', movie: '欢迎来龙餐馆', date: '2026-08-22', showtime: '09:55',
    official_selection: { is_selected: false, selected_seat_numbers: ['7排6座', '7排7座', '7排8座'], selected_count: 0 },
  });
});

test('structured buyer text calls the realtime quote API and makes no promise about typed seat numbers', async () => {
  const requests = [];
  const client = createQuotePreviewClient({
    quotePreview: {
      recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize',
      quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote',
      ingestKey: 'a'.repeat(32),
    },
  }, {
    async fetchImpl(url, input) {
      requests.push({ url: String(url), body: JSON.parse(input.body) });
      return new Response(JSON.stringify({
        quote_scope: 'area_probe', seat_zone_type: 'W+', member_unit_price_cents: 5490,
        unit_quote_cents: 7105, total_quote_cents: 7105, ticket_count: 1, needs_ticket_count: false,
        pricing_source: 'W+会员专享优惠', detail: 'area probe',
      }), { status: 200 });
    },
  });

  const result = await client.capture({
    id: 'event-text-quote', tenantId: 'tenant-1', ts: Date.parse('2026-08-18T10:00:00Z'),
    payload: { content: '8月22日 上海闵行颛桥万达 16:25 奥德赛 7排14座 7排15座' },
  });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, 'http://127.0.0.1:8010/api/quotes/preview-quote');
  assert.equal(requests[0].body.tenant_id, 'tenant-1');
  assert.equal(requests[0].body.recognition.official_selection.is_selected, false);
  assert.equal(result.status, 'preview_ready');
  assert.match(result.reply_text, /71.05元\/张/u);
  assert.match(result.reply_text, /文字里的座位号未按平台选座核验/u);
});

test('incomplete quote-like text asks only for the missing facts without calling the quote service', async () => {
  let called = false;
  const client = createQuotePreviewClient({
    quotePreview: {
      recognizeUrl: 'http://127.0.0.1:8010/api/quotes/preview-recognize',
      quoteUrl: 'http://127.0.0.1:8010/api/quotes/preview-quote',
      ingestKey: 'a'.repeat(32),
    },
  }, {
    async fetchImpl() { called = true; throw new Error('incomplete text must not call realtime quote'); },
  });

  const result = await client.capture({ id: 'event-no-facts', tenantId: 'tenant-1', payload: { content: '两张多少钱？' } });
  assert.equal(result.status, 'needs_confirmation');
  assert.deepEqual(result.missing_fields, ['日期（请使用未过期日期；过去日期请带年份）', '完整万达影院名', '开场时间', '影片名']);
  assert.match(result.reply_text, /请发送已标记需要购买位置的完整选座页截图/u);
  assert.match(result.reply_text, /需要补全：.*影院/u);
  assert.match(result.reply_text, /图片标记仅供人工出票/u);
  assert.equal(called, false);
});

