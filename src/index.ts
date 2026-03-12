// 配置
export { configure } from "./config.js";
export type { CccConfig } from "./config.js";

// 低层 tmux 操作
export {
  createSession,
  killSession,
  isAlive,
  listSessions,
  sendText,
  sendKey,
  capturePane,
  captureFull,
  resizePane,
} from "./transport.js";
export type { SendTextOptions } from "./transport.js";

// 高层 session API
export {
  runSession,
  ensureSession,
  killSession as killManagedSession,
  readState,
  waitReady,
  waitState,
  send,
  approvePermission,
} from "./session.js";
export type { PaneState, SendOptions, SessionConfig } from "./session.js";

// Parser（供高级用户直接使用）
export {
  stripAnsi,
  detectReady,
  detectPermission,
  detectChoices,
  extractLastResponse,
  detectModelPicker,
  detectCursorModelDropdown,
} from "./parser.js";
export type {
  ReadyResult,
  PermissionPrompt,
  ChoiceItem,
  PermissionOption,
  CursorDropdownResult,
} from "./parser.js";

// Relay
export { runRelay } from "./relay.js";
export type { RelayConfig, RelayResult, RelayMode, RelayRole, RelayTurn } from "./relay.js";

// Group Chat
export { runGroupChat } from "./groupchat.js";
export type { GroupChatConfig, AgentDef, ChatMessage } from "./groupchat.js";
