export async function readOfficialShops(client, tenantId, verified) {
  if (verified !== true || typeof tenantId !== 'string' || !tenantId || !client) throw new Error('SHOP_READ_UNAUTHORIZED');
  const rows = await client.shops.list();
  if (!Array.isArray(rows) || rows.some(s => !s || typeof s.unb !== 'string' || !s.unb || typeof s.enabled !== 'boolean')) throw new Error('SHOP_DIRECTORY_INVALID');
  return rows.map(s => ({tenant_id:tenantId, shop_id:s.unb,
    display_name:typeof s.displayName === 'string' ? s.displayName : typeof s.shopName === 'string' ? s.shopName : null,
    platform_status:typeof s.status === 'string' ? s.status : null,
    platform_enabled:s.enabled, im_enabled:s.imEnabled === true,
    area:typeof s.area === 'string' ? s.area : null}));
}
