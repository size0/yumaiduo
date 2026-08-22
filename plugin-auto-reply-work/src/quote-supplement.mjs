const KNOWN_CITY_NAMES = new Set([
  '北京', '上海', '天津', '重庆', '广州', '深圳', '成都', '杭州', '武汉', '西安', '南京', '苏州', '常州',
  '郑州', '长沙', '合肥', '济南', '青岛', '德州', '福州', '厦门', '莆田', '泉州', '漳州', '三明', '南平', '龙岩', '宁德', '南昌', '南宁', '昆明',
  '贵阳', '海口', '太原', '石家庄', '沈阳', '大连', '长春', '哈尔滨', '兰州', '银川',
  '西宁', '乌鲁木齐', '呼和浩特', '拉萨', '十堰', '淄博', '宁波', '张家港', '资中',
]);

const PROVINCE_NAMES = Object.freeze([
  '黑龙江', '内蒙古', '广西', '宁夏', '新疆', '西藏',
  '河北', '山西', '辽宁', '吉林', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南',
  '湖北', '湖南', '广东', '海南', '四川', '贵州', '云南', '陕西', '甘肃', '青海', '台湾',
]);

function withoutTrailingQuoteQuestion(value) {
  return String(value ?? '')
    .replace(/[？?。！!]+$/gu, '')
    .replace(/(?:多少钱(?:一张)?|多少|什么价(?:格)?|票价(?:多少)?|价格(?:多少)?|怎么卖|咋卖|能买吗)(?:啊|呢|呀|吗|吧)*$/u, '')
    .replace(/的$/u, '')
    .trim();
}

export function cityHintFromSupplement(value) {
  const lines = String(value ?? '').split(/\r?\n/u).map((line) => withoutTrailingQuoteQuestion(line.replace(/\s+/gu, '').trim())).filter(Boolean).slice(0, 12);
  for (const line of lines) {
    const labelled = line.match(/^(?:所在城市|城市|地区)[:：]?([\u4e00-\u9fff]{2,8}?)(?:市)?$/u);
    if (labelled) return labelled[1];
    const city = line.endsWith('市') ? line.slice(0, -1) : line;
    if (KNOWN_CITY_NAMES.has(city)) return city;
    const embeddedCity = [...KNOWN_CITY_NAMES].find((name) => (
      line.startsWith(name)
      && (/(?:万达|影城|影院|广场|天地|中心|店)/u.test(line.slice(name.length))
        || /^[A-Za-z0-9+·\-]{2,20}$/u.test(line.slice(name.length)))
    ));
    if (embeddedCity) return embeddedCity;
    const wanda = line.match(/^([\u4e00-\u9fff]{2,8}?)(?:市)?万达(?:影城|影院)?(?:[（(].*)?$/u);
    if (wanda && !['这个', '那个', '这里', '那边'].includes(wanda[1])) return wanda[1];
    const province = PROVINCE_NAMES.find((name) => line.startsWith(name) && line.length > name.length);
    if (province) {
      const provinceCity = line.slice(province.length).replace(/^省/u, '').replace(/市$/u, '');
      if (/^[\u4e00-\u9fff]{2,8}$/u.test(provinceCity)) return provinceCity;
    }
    const administrativeInput = line.replace(/^.*?(?:特别行政区|自治区|省)/u, '');
    const administrative = administrativeInput.match(/^([\u4e00-\u9fff]{2,10})市(?:[\u4e00-\u9fff]{1,12}(?:区|县|旗))?/u);
    if (administrative) {
      const cityName = administrative[1];
      if (/^[\u4e00-\u9fff]{2,10}$/u.test(cityName)) return cityName;
    }
  }
  return '';
}

export function cinemaHintFromSupplement(value) {
  const line = String(value ?? '').split(/\r?\n/u).map((item) => withoutTrailingQuoteQuestion(item.replace(/\s+/gu, ' ').trim())).find((item) => (
    item.length <= 160
    && item.includes('万达')
    && /(?:影城|影院|店|万达$)/u.test(item)
    && /^(?:[\u4e00-\u9fff]{1,20})?万达[\u4e00-\u9fffA-Za-z0-9+（）()·\- ]{0,120}$/u.test(item)
  ));
  if (line) return line.slice(0, 160);
  const compact = withoutTrailingQuoteQuestion(String(value ?? '').replace(/\s+/gu, ''));
  for (const match of compact.matchAll(/万达/gu)) {
    const suffix = compact.slice(Number(match.index) + 2);
    if (!/^(?:的)?万达(?:影城|影院)/u.test(suffix)) continue;
    const prefix = compact.slice(0, Number(match.index));
    const branchName = prefix.split(/(?:省|自治区|特别行政区|市|区|县|旗|[，,。；;、])/u).at(-1)?.replace(/^(?:你好|您好|问一下|咨询一下|请问)/u, '') ?? '';
    if (/^[\u4e00-\u9fffA-Za-z0-9+·\-]{2,20}$/u.test(branchName)) return `${branchName}万达`;
  }
  const branch = String(value ?? '').split(/\r?\n/u).map((item) => item.replace(/\s+/gu, '').trim()).find((item) => (
    /^[\u4e00-\u9fffA-Za-z0-9+·\-]{4,40}$/u.test(item)
    && /(?:广场|天地|中心|影城|影院|店)$/u.test(item)
    && !/^(?:这个|那个|这里|那边|这家|哪家|什么)/u.test(item)
  ));
  return branch ? branch.slice(0, 160) : '';
}

export function isLocationQuoteSupplement(payload) {
  const text = payload?.content ?? payload?.text;
  return Boolean(cityHintFromSupplement(text) || cinemaHintFromSupplement(text));
}
