import { v4 as uuid } from "uuid";

/** 网络结果未知时重发同一份内容；新业务动作必须显式结束上一轮。 */
export class RequestIdentity {
  private attempt: { body: string; id: string } | null = null;
  for(body: unknown): string {
    const serialized = JSON.stringify(body);
    if (this.attempt && this.attempt.body !== serialized)
      throw new Error("上次请求结果尚未确认，请按原内容重试或先查询进度");
    this.attempt ??= { body: serialized, id: uuid() };
    return this.attempt.id;
  }
  reset(): void {
    this.attempt = null;
  }
}
