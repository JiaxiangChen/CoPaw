let lastTimestamp = 0;
let sequence = 0;

export function createLocalSessionId(): string {
  const now = Date.now();
  sequence = now === lastTimestamp ? sequence + 1 : 0;
  lastTimestamp = now;
  return `${now}${sequence.toString().padStart(3, "0")}`;
}
