export interface CccConfig {
  /** tmux session 名前缀。默认 "ccc-" */
  sessionPrefix: string;
}

const _config: CccConfig = { sessionPrefix: "ccc-" };

export function configure(opts: Partial<CccConfig>): void {
  Object.assign(_config, opts);
}

export function getConfig(): CccConfig {
  return _config;
}
