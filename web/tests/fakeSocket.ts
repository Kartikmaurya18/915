/** A scriptable stand-in for WebSocket: tests drive open/message/close and inspect what was sent. */
export class FakeSocket {
  static instances: FakeSocket[] = [];
  static reset(): void {
    FakeSocket.instances = [];
  }
  static get last(): FakeSocket {
    return FakeSocket.instances[FakeSocket.instances.length - 1]!;
  }

  readyState = 0;
  sent: Record<string, unknown>[] = [];
  closedWith: { code?: number; reason?: string } | null = null;
  onopen: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }
  close(code?: number, reason?: string): void {
    this.closedWith = { code, reason };
    this.readyState = 3;
    this.onclose?.({ code: code ?? 1005 });
  }

  // -- test helpers --
  open(): void {
    this.readyState = 1;
    this.onopen?.({});
  }
  receive(msg: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  serverClose(code: number): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  sentTypes(): string[] {
    return this.sent.map((m) => String(m.type));
  }
}
