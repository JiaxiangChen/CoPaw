import { describe, expect, it } from "vitest";
import { convertArchivedPage, convertMessages } from "./index";
import {
  INLINE_COMPACTION_TYPE,
  mergeHistoryResponses,
  reconcileHistoryLayers,
  normalizeHistoryMessages,
  resolveHistoryAnchorId,
} from "./historyAssembly";

const boundary = {
  id: "boundary",
  archived_message_count: 2,
  first_message_id: "question",
  last_message_id: "call",
  created_at: "2026-10-28T01:00:00Z",
};
const call = {
  id: "call",
  role: "assistant",
  type: "plugin_call",
  content: [{ type: "data", data: { call_id: "tool-a", arguments: "{}" } }],
};
const output = {
  id: "output",
  role: "system",
  type: "plugin_call_output",
  content: [{ type: "data", data: { call_id: "tool-a", output: "ok" } }],
};
const historical = (messages: ReturnType<typeof convertMessages>) =>
  messages.map((message) => ({ ...message, history: true }));

describe("archived history assembly", () => {
  it("keeps retained users before a response matched by fragment identity", () => {
    const laterUser = { id: "q2", role: "user", content: "next" };
    const retained = convertArchivedPage(
      [
        { id: "q1", role: "user", content: "question" },
        call,
        output,
        laterUser,
      ],
      [],
    );
    const newer = convertArchivedPage(
      [{ ...output, content: "latest" }, laterUser],
      [],
    );
    const result = reconcileHistoryLayers(retained, newer);
    expect(result.map((message) => message.id)).toEqual([
      "q1",
      newer[0].id,
      "q2",
    ]);
    expect(
      result[1].cards![0].data.output.map(
        (message: { id: string }) => message.id,
      ),
    ).toEqual(["call", "output"]);
    expect(result[1].cards![0].data.output[1].content).toBe("latest");
  });

  it("keeps a trailing archive divider inside its online response before a shared user", () => {
    const laterUser = { id: "q2", role: "user", content: "next" };
    const retained = convertArchivedPage([call, laterUser], [boundary]);
    const newer = convertArchivedPage(
      [
        { id: "q1", role: "user", content: "question" },
        call,
        output,
        laterUser,
      ],
      [],
    );
    const result = reconcileHistoryLayers(retained, newer);
    expect(result.map((message) => message.id)).toEqual(
      newer.map((message) => message.id),
    );
    expect(
      result[1].cards![0].data.output.map(
        (message: { id: string }) => message.id,
      ),
    ).toEqual(["call", "conversation-compaction-boundary", "output"]);
    expect(reconcileHistoryLayers(retained, result)).toEqual(result);
  });

  it("keeps a shared trailing divider only once when reconciling a layer with itself", () => {
    const layer = convertArchivedPage([call], [boundary]);
    const result = reconcileHistoryLayers(layer, layer);

    expect(result.map((message) => message.id)).toEqual(
      layer.map((message) => message.id),
    );
    expect(
      result[0].cards![0].data.output.map((item: { id: string }) => item.id),
    ).toEqual(["call"]);
  });

  it("preserves distinct trailing dividers from both layers", () => {
    const retained = convertArchivedPage([call], [boundary]);
    const newer = convertArchivedPage([call], [{ ...boundary, id: "other" }]);
    const result = reconcileHistoryLayers(retained, newer);

    expect(
      result[0].cards![0].data.output.map((item: { id: string }) => item.id),
    ).toEqual(["call", "conversation-compaction-boundary"]);
    expect(result[1].id).toBe("conversation-compaction-other");
  });

  it("aligns retained history with a refreshed traversal that reaches an older prefix", () => {
    const user = (id: string) =>
      historical(convertMessages([{ id, role: "user", content: id }]))[0];
    const retained = [user("b"), user("c")];
    const traversal = [user("a"), user("b"), user("c"), user("d")];
    expect(
      reconcileHistoryLayers(retained, traversal).map((message) => message.id),
    ).toEqual(["a", "b", "c", "d"]);
    expect(
      reconcileHistoryLayers(retained, [user("d")]).map(
        (message) => message.id,
      ),
    ).toEqual(["b", "c", "d"]);
  });

  it("retains descendant aliases when an identical response is refreshed", () => {
    const older = convertArchivedPage([call], []);
    const newer = convertArchivedPage([output], []);
    const retained = mergeHistoryResponses([...older, ...newer]);
    const refreshed = reconcileHistoryLayers(retained, newer);
    expect(
      resolveHistoryAnchorId(
        refreshed,
        "process:" + older[0].cards![0].data.id,
      ),
    ).toBe("process:" + newer[0].cards![0].data.id);
    expect(refreshed[0].cards![0].data.output).toHaveLength(2);
  });
  it("keeps legacy fragments with shared IDs and stable source identity", () => {
    const raw = [
      { ...call, id: "source" },
      {
        ...call,
        id: "source",
        content: [{ type: "data", data: { call_id: "tool-b" } }],
      },
      {
        id: "source",
        role: "assistant",
        type: "reasoning",
        content: "thinking",
      },
    ].map((message) => ({ ...message, metadata: { original_id: "source" } }));
    const messages = normalizeHistoryMessages(raw);
    expect(new Set(messages.map((message) => message.id)).size).toBe(3);
    expect(messages.map((message) => message.metadata)).toEqual([
      { original_id: "source" },
      { original_id: "source" },
      { original_id: "source" },
    ]);
    expect(normalizeHistoryMessages(messages)).toEqual(messages);
    expect(raw.map((message) => message.id)).toEqual([
      "source",
      "source",
      "source",
    ]);
  });

  it("assembles one response across a page and compaction boundary", () => {
    const older = convertArchivedPage(
      [{ id: "question", role: "user", content: "question" }, call],
      [boundary],
    );
    const newer = historical(
      convertMessages([
        output,
        { id: "answer", role: "assistant", content: "answer" },
      ]),
    );
    const oldAnchor = older[1].id;
    const merged = mergeHistoryResponses([...historical(older), ...newer]);
    expect(merged.map((message) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
    expect(merged[1].id).toBe(newer[0].id);
    expect(
      merged[1].cards?.[0].data.output.map(
        (item: { type?: string }) => item.type,
      ),
    ).toEqual([
      "plugin_call",
      INLINE_COMPACTION_TYPE,
      "plugin_call_output",
      undefined,
    ]);
    expect(resolveHistoryAnchorId(merged, oldAnchor)).toBe(newer[0].id);
    expect(mergeHistoryResponses(merged)).toEqual(merged);
  });

  it("does not make a compression boundary a new response within one page", () => {
    const result = convertArchivedPage(
      [call, output],
      [{ ...boundary, last_message_id: "call" }],
    );
    expect(result).toHaveLength(1);
    expect(result[0].cards?.[0].data.output).toHaveLength(3);
  });

  it("resolves process anchors through repeated response merges", () => {
    const older = convertArchivedPage([call], []);
    const newer = convertArchivedPage([output], []);
    const processAnchor = (message: (typeof older)[number]) =>
      `process:${message.cards![0].data.id}`;
    expect(older[0].id).not.toBe(older[0].cards![0].data.id);
    const merged = mergeHistoryResponses([...older, ...newer]);
    expect(resolveHistoryAnchorId(merged, processAnchor(older[0]))).toBe(
      processAnchor(newer[0]),
    );
    const latest = convertArchivedPage(
      [{ id: "answer", role: "assistant", content: "answer" }],
      [],
    );
    const remerged = mergeHistoryResponses([...merged, ...latest]);
    for (const response of [...older, ...newer, ...latest]) {
      expect(resolveHistoryAnchorId(remerged, processAnchor(response))).toBe(
        processAnchor(latest[0]),
      );
    }
    expect(resolveHistoryAnchorId(remerged, older[0].id)).toBe(latest[0].id);
    expect(resolveHistoryAnchorId(remerged, "process:unknown")).toBe(
      "process:unknown",
    );
    expect(resolveHistoryAnchorId(remerged, call.id)).toBe(call.id);
  });

  it("keeps user, standalone Stop and live response barriers", () => {
    const first = historical(convertMessages([call]));
    const second = historical(
      convertMessages([{ id: "q2", role: "user", content: "next" }, output]),
    );
    expect(mergeHistoryResponses([...first, ...second])).toHaveLength(3);
    const stop = historical(
      convertMessages([
        {
          id: "append",
          role: "assistant",
          content: "append",
          metadata: { stop_append: { message_id: "append", order: 1 } },
        },
      ]),
    );
    expect(
      mergeHistoryResponses([
        ...first,
        ...stop,
        ...historical(convertMessages([output])),
      ]),
    ).toHaveLength(3);
    expect(
      mergeHistoryResponses([...first, ...convertMessages([output])]),
    ).toHaveLength(2);
  });

  it("uses the same legacy fragment ID in singleton and overlapping pages", () => {
    const first = {
      ...call,
      id: "source",
      metadata: { original_id: "source" },
    };
    const second = { ...first, type: "reasoning", content: "thinking" };
    expect(normalizeHistoryMessages([first])[0].id).toBe(
      normalizeHistoryMessages([second, first])[1].id,
    );
    const normalized = normalizeHistoryMessages([first, { ...first }]);
    expect(normalized[0].id).toBe(normalized[1].id);
  });

  it("keeps distinct task executions separate within a page and across pages", () => {
    const tasks = [
      {
        id: "task-a",
        role: "assistant",
        content: "A",
        metadata: { task_run_id: "run-a" },
      },
      {
        id: "task-b",
        role: "assistant",
        content: "B",
        metadata: { metadata: { task_run_id: "run-b" } },
      },
    ];
    expect(convertArchivedPage(tasks, [])).toHaveLength(2);
    expect(
      mergeHistoryResponses(
        tasks.flatMap((task) => convertArchivedPage([task], [])),
      ),
    ).toHaveLength(2);
  });

  it("removes duplicated response fragments across an online user boundary", () => {
    const user = convertMessages([
      { id: "question", role: "user", content: "question" },
    ])[0];
    const archived = convertMessages([
      { id: "answer-1", role: "assistant", content: "first answer" },
    ])[0];
    const online = convertMessages([
      { id: "answer-1", role: "assistant", content: "first answer" },
      { id: "answer-2", role: "assistant", content: "second answer" },
    ])[0];

    const merged = mergeHistoryResponses(historical([archived, user, online]));

    expect(merged).toHaveLength(2);
    expect(merged[0].role).toBe("user");
    expect(merged[1].cards?.[0].data.output).toHaveLength(2);
  });

  it("preserves unique overlap fragments, newer values and both anchor namespaces", () => {
    const older = convertArchivedPage([call, output], [boundary]);
    const newer = historical(
      convertMessages([
        { id: "question", role: "user", content: "question" },
        {
          ...output,
          content: [
            { type: "data", data: { call_id: "tool-a", output: "latest" } },
          ],
        },
        { id: "answer", role: "assistant", content: "answer" },
      ]),
    );
    const merged = mergeHistoryResponses([...older, ...newer]);
    expect(merged.map((message) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
    const items = merged[1].cards![0].data.output;
    expect(items.map((item: { id: string }) => item.id)).toEqual([
      "call",
      "conversation-compaction-boundary",
      "output",
      "answer",
    ]);
    expect(items[2].content[0].data.output).toBe("latest");
    expect(resolveHistoryAnchorId(merged, older[0].id)).toBe(newer[1].id);
    expect(
      resolveHistoryAnchorId(merged, "process:" + older[0].cards![0].data.id),
    ).toBe("process:" + newer[1].cards![0].data.id);
    expect(mergeHistoryResponses(merged)).toEqual(merged);
  });

  it("keeps explicit conflicting turn IDs separate, including nested metadata", () => {
    const turns = [
      { ...call, metadata: { turn_id: "turn-a" } },
      { ...output, metadata: { metadata: { turn_id: "turn-b" } } },
    ];
    expect(convertArchivedPage(turns, [])).toHaveLength(2);
    expect(
      mergeHistoryResponses(
        turns.flatMap((message) => convertArchivedPage([message], [])),
      ),
    ).toHaveLength(2);
  });

  it("keeps the newer response order when its overlap includes an earlier prefix", () => {
    const older = convertArchivedPage([output], []);
    const newer = convertArchivedPage([call, output], []);
    const merged = mergeHistoryResponses([...older, ...newer]);
    expect(
      merged[0].cards![0].data.output.map((item: { id: string }) => item.id),
    ).toEqual(["call", "output"]);
  });

  it("moves trailing archive dividers with their overlapping response", () => {
    const older = convertArchivedPage([call], [boundary]);
    const newer = historical(
      convertMessages([
        { id: "question", role: "user", content: "question" },
        call,
        output,
      ]),
    );
    const merged = mergeHistoryResponses([...older, ...newer]);
    expect(merged.map((message) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
    expect(
      merged[1].cards![0].data.output.map((item: { id: string }) => item.id),
    ).toEqual(["call", "conversation-compaction-boundary", "output"]);
  });

  it("allows legacy fragments without turn IDs to join a known turn", () => {
    const turns = [call, { ...output, metadata: { turn_id: "turn-a" } }];
    expect(convertArchivedPage(turns, [])).toHaveLength(1);
    expect(
      mergeHistoryResponses(
        turns.flatMap((message) => convertArchivedPage([message], [])),
      ),
    ).toHaveLength(1);
  });

  it("does not reconcile shared IDs across explicit turns or live responses", () => {
    const first = convertArchivedPage(
      [{ ...call, metadata: { turn_id: "a" } }],
      [],
    );
    const second = convertArchivedPage(
      [{ ...call, metadata: { turn_id: "b" } }],
      [],
    );
    expect(mergeHistoryResponses([...first, ...second])).toHaveLength(2);
    const live = { ...first[0], msgStatus: "generating" as const };
    expect(mergeHistoryResponses([...first, live])).toHaveLength(2);
  });

  it("keeps tools from the same explicit task run in one response", () => {
    const tasks = [
      { ...call, metadata: { task_run_id: "run-a" } },
      { ...output, metadata: { metadata: { task_run_id: "run-a" } } },
    ];
    expect(convertArchivedPage(tasks, [])).toHaveLength(1);
    expect(
      mergeHistoryResponses(
        tasks.flatMap((task) => convertArchivedPage([task], [])),
      ),
    ).toHaveLength(1);
  });
});
