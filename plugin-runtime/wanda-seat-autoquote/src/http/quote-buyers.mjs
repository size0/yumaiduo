// Display-only enrichment. Never use a buyer identifier as a nickname.
export function createQuoteBuyerEnricher(platform, { ttlMs = 300_000, timeoutMs = 2500 } = {}) {
  const cache = new Map();
  return async function enrich(tenantId, records) {
    const output = records.map(record => ({ ...record }));
    const groups = new Map();
    for (const record of output) {
      const shop = String(record.shop_id || '').trim();
      const buyer = String(record.buyer_id || '').trim();
      if (!shop || !buyer) continue;
      const key = JSON.stringify([String(tenantId), shop, buyer]);
      if (!groups.has(key)) groups.set(key, { shop, buyer, records: [] });
      groups.get(key).records.push(record);
    }
    const jobs = [...groups.entries()];
    const deadline = Date.now() + 8000;
    async function worker() {
      while (jobs.length && Date.now() < deadline) {
        const [key, group] = jobs.shift();
        let entry = cache.get(key);
        if (!entry || entry.expires <= Date.now()) {
          let timer;
          let nick = '';
          try {
            const result = await Promise.race([
              platform.createClient(String(tenantId)).im.getSessionByPeer(group.shop, group.buyer),
              new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('nickname_timeout')), timeoutMs); }),
            ]);
            const session = result?.data?.session ?? result?.session ?? result?.data ?? result;
            if (String(session?.accountUnb || '') === group.shop && String(session?.peerUnb || '') === group.buyer) {
              nick = typeof session.peerNick === 'string' ? session.peerNick.trim() : '';
              if (nick === group.buyer) nick = '';
            }
          } catch { /* Nickname failure must not hide quote records. */ }
          finally { clearTimeout(timer); }
          entry = { nick, expires: Date.now() + (nick ? ttlMs : 30_000) };
          if (cache.size >= 2000) cache.delete(cache.keys().next().value);
          cache.set(key, entry);
        }
        if (entry.nick) for (const record of group.records) record.user_name = record.buyer_nick = entry.nick;
      }
    }
    await Promise.all(Array.from({ length: Math.min(6, jobs.length) }, worker));
    return output;
  };
}
