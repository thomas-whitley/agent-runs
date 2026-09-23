// One JSON object per line on stdout, with the fields app/logging_setup.py
// writes (timestamp, level, logger, message, plus run_id and worker_id), and
// executor so a line says which executor wrote it. No trace or span ids:
// this process has no tracer.
//
// The last lines are kept so a failed check can send them with its result,
// which makes it debuggable from the runs page without touching this
// machine. A success sends none.

export type Fields = Record<string, unknown>;

export interface Logger {
  info(message: string, fields?: Fields): void;
  warn(message: string, fields?: Fields): void;
  error(message: string, fields?: Fields): void;
  tail(): string[];
}

export interface LoggerOptions {
  workerId: string;
  write?: (line: string) => void;
  keep?: number;
}

export function createLogger(options: LoggerOptions): Logger {
  const write = options.write ?? ((line: string) => process.stdout.write(`${line}\n`));
  const keep = options.keep ?? 50;
  const recent: string[] = [];

  function emit(level: string, message: string, fields: Fields = {}): void {
    const line = JSON.stringify({
      timestamp: new Date().toISOString(),
      level,
      logger: "agent_runs.checks",
      message,
      worker_id: options.workerId,
      executor: "self_hosted",
      ...fields,
    });
    recent.push(line);
    if (recent.length > keep) {
      recent.shift();
    }
    write(line);
  }

  return {
    info: (message, fields) => emit("INFO", message, fields),
    warn: (message, fields) => emit("WARNING", message, fields),
    error: (message, fields) => emit("ERROR", message, fields),
    tail: () => [...recent],
  };
}
