function definition(implementation, effect, { requires = [], reads = [], writes = [] } = {}) {
  return Object.freeze({ implementation, effect, requires: Object.freeze(requires), reads: Object.freeze(reads), writes: Object.freeze(writes) });
}

const DEFINITIONS = Object.freeze({
  recognize_image: definition('recognize_image', 'read', { reads: ['current_message_images'] }),
  resolve_ticket_identity: definition('resolve_ticket_identity', 'read', { reads: ['conversation_identity'] }),
  resolve_showtime: definition('resolve_showtime', 'read', { requires: ['recognized_identity'], reads: ['showtime_catalog'] }),
  quote_realtime: definition('quote_realtime', 'external_temporary_write', { requires: ['unique_showtime'], reads: ['wanda_seats'], writes: ['temporary_quote_probe'] }),
  read_active_quote: definition('read_active_quote', 'read', { reads: ['active_quote'] }),
  show_available_wplus_seats: definition('list_available_wplus_seats', 'read', { requires: ['unique_showtime'], reads: ['wanda_seats'] }),
  record_seat_preference: definition('record_seat_preference', 'write', { writes: ['conversation_preference'] }),
  confirm_active_quote: definition('confirm_active_quote', 'write', { requires: ['explicit_current_confirmation'], reads: ['active_quote'], writes: ['quote_confirmation'] }),
  read_linked_order: definition('read_linked_order', 'read', { reads: ['linked_order'] }),
  change_order_price: definition('change_order_price', 'write', { requires: ['confirmed_quote', 'linked_unpaid_order'], reads: ['active_quote', 'linked_order'], writes: ['order_price'] }),
  create_manual_task: definition('create_manual_task', 'write', { writes: ['manual_task'] }),
  get_manual_task_status: definition('get_manual_task_status', 'read', { reads: ['manual_task'] }),
});

export const NATIVE_AGENT_TOOL_NAMES = Object.freeze(Object.keys(DEFINITIONS));

export function nativeToolDefinition(nameValue) {
  return DEFINITIONS[String(nameValue ?? '')] ?? null;
}

export function nativeToolIsReadOnly(name) {
  return nativeToolDefinition(name)?.effect === 'read';
}

export function nativeToolsCanRunInParallel(calls) {
  const definitions = (Array.isArray(calls) ? calls : []).map((call) => nativeToolDefinition(call?.function?.name));
  return definitions.length > 0 && definitions.every((item) => item?.effect === 'read' && item.requires.length === 0 && item.writes.length === 0);
}

export function resolveNativeTool(tools, name) {
  const definition = nativeToolDefinition(name);
  if (!definition) return null;
  const implementation = tools?.[definition.implementation];
  return typeof implementation === 'function' ? implementation : null;
}
