export function formatDuration(seconds: number | null, status: string): string {
  if (seconds === null) return status === "pending" || status === "running" ? "in progress" : "";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} m ${Math.round(seconds - minutes * 60)} s`;
}

export function formatTokens(tokens: number): string {
  return tokens.toLocaleString("en-AU");
}

export function formatCreated(iso: string): string {
  return new Date(iso).toLocaleString("en-AU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
