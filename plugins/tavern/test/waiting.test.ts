import { describe, expect, it } from "vitest";
import { candidates, classify, hookLine, lastQuestion, rankWaiting, type WaitingInput } from "../lib/waiting";

const base: WaitingInput = {
  id: "t",
  title: "тред",
  updatedAt: 1000,
  status: "idle",
  hasPendingInteraction: false,
  lastReadAt: 2000,
  latestAttentionAt: 1500,
  archived: false,
  hidden: false,
  child: false,
};

describe("ждут тебя", () => {
  it("причины по порядку важности", () => {
    expect(classify({ ...base, status: "error", lastReadAt: 100 }, "Вопрос?")).toBe("error");
    // ревью: прочитанная ошибка не висит весь период
    expect(classify({ ...base, status: "error" }, null)).toBeNull();
    expect(classify({ ...base, hasPendingInteraction: true }, null)).toBe("pending");
    expect(classify(base, "Сделал. Выкатываем сейчас или после ревью?")).toBe("question");
    expect(classify({ ...base, lastReadAt: 100 }, "Готово.")).toBe("unread");
    expect(classify({ ...base, lastReadAt: null }, "Готово.")).toBe("unread");
    expect(classify(base, "Готово, всё проверено.")).toBeNull();
  });

  it("ревью: риторический вопрос в начале, ссылка с ? и код — не вопрос; вопрос в конце длинного ответа — вопрос", () => {
    expect(lastQuestion("Почему падало? Причина в кеше. Починил, тесты зелёные. Всё выкатил.")).toBeNull();
    expect(lastQuestion("Смотри https://x.com/a?q=1 и `a ? b : c`. Готово.")).toBeNull();
    expect(lastQuestion("Пример:\n```\nfoo?\n```\nВсё работает.")).toBeNull();
    expect(lastQuestion("а".repeat(500) + ". Выкатываем сейчас?")).toBe("Выкатываем сейчас?");
  });

  it("строка — последний вопрос агента, иначе начало ответа", () => {
    expect(lastQuestion("Нашёл две причины. Чинить первую? Или обе?")).toBe("Или обе?");
    expect(hookLine("Готово: плагин стоит. Тесты зелёные.")).toBe("Готово: плагин стоит.");
    expect(hookLine(null)).toBe("");
    expect(hookLine("а".repeat(300)).length).toBe(140);
  });

  it("кандидаты: без архива, скрытых, сабагентов, старых и крутящихся (кроме ждущих ответа)", () => {
    const ts: WaitingInput[] = [
      { ...base, id: "ok" },
      { ...base, id: "arch", archived: true },
      { ...base, id: "hid", hidden: true },
      { ...base, id: "kid", child: true },
      { ...base, id: "old", updatedAt: 10 },
      { ...base, id: "run", status: "active" },
      { ...base, id: "ask", status: "active", hasPendingInteraction: true },
    ];
    expect(candidates(ts, 500).map((t) => t.id)).toEqual(["ok", "ask"]);
  });

  it("сортировка: ошибки выше непрочитанного, внутри — свежие выше", () => {
    const r = rankWaiting(
      [
        { threadId: "u", title: "", reason: "unread", line: "", updatedAt: 9 },
        { threadId: "e", title: "", reason: "error", line: "", updatedAt: 1 },
        { threadId: "q1", title: "", reason: "question", line: "", updatedAt: 2 },
        { threadId: "q2", title: "", reason: "question", line: "", updatedAt: 5 },
      ],
      3,
    );
    expect(r.map((x) => x.threadId)).toEqual(["e", "q2", "q1"]);
  });
});
