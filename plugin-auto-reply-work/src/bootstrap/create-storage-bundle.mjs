import path from 'node:path';
import { FileEventStore } from '../event-store.mjs';
import { ConversationContextStore } from '../conversation-context-store.mjs';
import { AgentEvaluationStore } from '../agent-evaluation-store.mjs';
import { AgentRunStore } from '../agent/agent-run-store.mjs';
import { AgentReplyOutboxStore } from '../agent/agent-reply-outbox-store.mjs';
import { AgentManualTaskStore } from '../agent/agent-manual-task-store.mjs';
import { AgentHumanComparisonStore } from '../agent/agent-human-comparison-store.mjs';

/** Construct the isolated durable stores used by one plugin application. */
export function createStorageBundle({ dataDir, configEncryptionKey = null, eventRetentionDays } = {}) {
  if (typeof dataDir !== 'string' || !dataDir.trim()) throw new TypeError('storage dataDir is required');
  const retentionDays = Number(eventRetentionDays);
  if (!Number.isSafeInteger(retentionDays) || retentionDays < 1 || retentionDays > 365) {
    throw new TypeError('eventRetentionDays must be an integer between 1 and 365');
  }
  const root = path.resolve(dataDir);
  const eventStore = new FileEventStore(path.join(root, 'events.json'), {
    encryptionKey: configEncryptionKey,
    retentionMs: retentionDays * 24 * 60 * 60 * 1_000,
  });
  const conversationContextStore = new ConversationContextStore(path.join(root, 'conversation-context.json'));
  const agentEvaluationStore = new AgentEvaluationStore(path.join(root, 'agent-evaluations.json'));
  const agentRunStore = new AgentRunStore(path.join(root, 'agent-runs.json'));
  const agentReplyOutboxStore = new AgentReplyOutboxStore(path.join(root, 'agent-reply-outbox.json'));
  const agentManualTaskStore = new AgentManualTaskStore(path.join(root, 'agent-manual-tasks.json'));
  const agentHumanComparisonStore = new AgentHumanComparisonStore(path.join(root, 'agent-human-comparisons.json'));

  async function initialize() {
    await Promise.all([
      eventStore.initialize(),
      agentRunStore.initialize(),
      agentReplyOutboxStore.initialize(),
      agentManualTaskStore.initialize(),
    ]);
  }

  return Object.freeze({
    eventStore,
    conversationContextStore,
    agentEvaluationStore,
    agentRunStore,
    agentReplyOutboxStore,
    agentManualTaskStore,
    agentHumanComparisonStore,
    initialize,
  });
}
