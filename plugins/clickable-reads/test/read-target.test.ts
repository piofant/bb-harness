import { describe, expect, it } from "vitest";
import {
  describeReadError,
  fileViewFor,
  findRowById,
  readTargetForRow,
  resolveReadPath,
  type TimelineRowLike,
} from "../lib/read-target";

const fileRead = (id: string, path: string | null, status = "completed"): TimelineRowLike => ({
  id,
  kind: "work",
  workKind: "file-read",
  status,
  path,
});

const command = (
  id: string,
  intents: { type: string; path?: string | null }[],
  cwd: string | null = "/Users/me/project",
): TimelineRowLike => ({
  id,
  kind: "work",
  workKind: "command",
  status: "completed",
  cwd,
  activityIntents: intents,
});

describe("findRowById", () => {
  const rows: TimelineRowLike[] = [
    { id: "thr_a:user-seed:1", kind: "conversation" },
    {
      id: "thr_a:t1:turn:0",
      kind: "turn",
      children: [
        fileRead("thr_a:file-read:i1", "/a.png"),
        {
          id: "thr_a:delegation:i5",
          kind: "work",
          workKind: "delegation",
          childRows: [command("thr_a:delegation:i5:child:thr_a:command:i6", [])],
        },
      ],
    },
    { id: "thr_a:t2:turn:0", kind: "turn", children: null },
  ];

  it("finds top-level, turn and delegation rows", () => {
    expect(findRowById(rows, "thr_a:user-seed:1")?.kind).toBe("conversation");
    expect(findRowById(rows, "thr_a:file-read:i1")?.path).toBe("/a.png");
    expect(findRowById(rows, "thr_a:delegation:i5:child:thr_a:command:i6")?.workKind).toBe(
      "command",
    );
  });

  it("returns null for a missing row", () => {
    expect(findRowById(rows, "thr_a:file-read:missing")).toBeNull();
    expect(findRowById([], "thr_a:file-read:i1")).toBeNull();
  });
});

describe("readTargetForRow", () => {
  it("uses a file read's absolute path", () => {
    expect(readTargetForRow(fileRead("r", "/home/me/x/../y/a.png"))).toEqual({
      kind: "path",
      path: "/home/me/y/a.png",
    });
  });

  it("reports a file read without a path", () => {
    expect(readTargetForRow(fileRead("r", null)).kind).toBe("error");
  });

  it("resolves a command's relative read against its folder", () => {
    expect(readTargetForRow(command("c", [{ type: "read", path: "AGENTS.md" }]))).toEqual({
      kind: "path",
      path: "/Users/me/project/AGENTS.md",
    });
    expect(
      readTargetForRow(command("c", [{ type: "read", path: "/etc/hosts" }], null)),
    ).toEqual({ kind: "path", path: "/etc/hosts" });
  });

  it("takes a file read's path literally, even with $ in the name", () => {
    expect(readTargetForRow(fileRead("r", "/notes/price$.md"))).toEqual({
      kind: "path",
      path: "/notes/price$.md",
    });
  });

  it("refuses reads the user denied or has not approved yet", () => {
    for (const approvalStatus of ["denied", "waiting_for_approval"]) {
      expect(
        readTargetForRow({
          ...command("c", [{ type: "read", path: "/home/me/.aws/credentials" }]),
          approvalStatus,
        }),
      ).toEqual({ kind: "error", message: "Это чтение не было разрешено, файл не открываю." });
    }
  });

  it("opens only reads that finished", () => {
    expect(readTargetForRow(fileRead("r", "/a.txt", "pending"))).toEqual({
      kind: "error",
      message: "Агент ещё не дочитал этот файл — открой чуть позже.",
    });
    expect(readTargetForRow(fileRead("r", "/a.txt", "error"))).toEqual({
      kind: "error",
      message: "Агент не прочитал этот файл, поэтому он не открывается.",
    });
    expect(
      readTargetForRow({ ...command("c", [{ type: "read", path: "/a.txt" }]), status: "interrupted" }).kind,
    ).toBe("error");
  });

  it("expands ~ and $HOME from the home folder the command ran in", () => {
    const skill = { type: "read", path: "~/.claude/skills/skill-scout/scripts/scout.sh" };
    expect(readTargetForRow(command("c", [skill], "/home/me/work"))).toEqual({
      kind: "path",
      path: "/home/me/.claude/skills/skill-scout/scripts/scout.sh",
    });
    expect(readTargetForRow(command("c", [skill], "/Users/me/cursor/lab"))).toEqual({
      kind: "path",
      path: "/Users/me/.claude/skills/skill-scout/scripts/scout.sh",
    });
    expect(readTargetForRow(command("c", [{ type: "read", path: "$HOME/a.md" }], "/home/user/x"))).toEqual({
      kind: "path",
      path: "/home/user/a.md",
    });
    expect(readTargetForRow(command("c", [{ type: "read", path: "${HOME}/a.md" }], "/root"))).toEqual({
      kind: "path",
      path: "/root/a.md",
    });
  });

  it("prefers the host's known home over a guess from the folder", () => {
    expect(
      readTargetForRow(command("c", [{ type: "read", path: "~/notes.md" }], "/home/ubuntu/app"), "/root"),
    ).toEqual({ kind: "path", path: "/root/notes.md" });
  });

  it("falls back to the host's home when the folder does not show it", () => {
    expect(
      readTargetForRow(command("c", [{ type: "read", path: "~/a.md" }], "/opt/work"), "/srv/me"),
    ).toEqual({ kind: "path", path: "/srv/me/a.md" });
    expect(readTargetForRow(command("c", [{ type: "read", path: "~/a.md" }], "/opt/work"))).toEqual({
      kind: "error",
      message: "Путь «~/a.md» начинается с домашней папки, а её на машине треда определить не удалось.",
    });
  });

  it("refuses paths it cannot place", () => {
    expect(readTargetForRow(command("c", [{ type: "read", path: "$TMPDIR/zan.txt" }]))).toEqual({
      kind: "error",
      message: "Путь «$TMPDIR/zan.txt» зависит от переменной окружения, её значение неизвестно.",
    });
    expect(readTargetForRow(command("c", [{ type: "read", path: "~other/notes.md" }])).kind).toBe(
      "error",
    );
    expect(readTargetForRow(command("c", [{ type: "read", path: "notes.md" }], null)).kind).toBe(
      "error",
    );
  });

  it("reports a command that read several files", () => {
    const target = readTargetForRow(
      command("c", [
        { type: "read", path: "/a.md" },
        { type: "read", path: "/b.md" },
      ]),
    );
    expect(target).toEqual({
      kind: "error",
      message: "Команда прочитала несколько файлов: /a.md, /b.md.",
    });
  });

  it("treats searches, listings and other rows as not a read", () => {
    expect(readTargetForRow(command("c", [{ type: "search", path: "/a.md" }]))).toEqual({
      kind: "not-read",
    });
    expect(readTargetForRow(command("c", [{ type: "read", path: null }]))).toEqual({
      kind: "not-read",
    });
    expect(readTargetForRow({ id: "x", kind: "conversation" })).toEqual({ kind: "not-read" });
    expect(readTargetForRow({ id: "x", kind: "work", workKind: "file-change" })).toEqual({
      kind: "not-read",
    });
  });
});

describe("resolveReadPath", () => {
  it("keeps Windows absolute paths", () => {
    expect(resolveReadPath("C:\\work\\a.txt", null)).toEqual({
      kind: "path",
      path: "C:\\work\\a.txt",
    });
  });
});

describe("fileViewFor", () => {
  it("shows images by extension or mime type", () => {
    expect(fileViewFor("/a/B.PNG", undefined, "base64")).toEqual({
      kind: "image",
      mimeType: "image/png",
    });
    expect(fileViewFor("/a/logo.svg", "image/svg+xml", "utf8")).toEqual({
      kind: "image",
      mimeType: "image/svg+xml",
    });
    expect(fileViewFor("/a/shot", "image/webp", "base64")).toEqual({
      kind: "image",
      mimeType: "image/webp",
    });
  });

  it("shows utf8 files as text and the rest as other", () => {
    expect(fileViewFor("/a/notes.md", "text/markdown", "utf8")).toEqual({ kind: "text" });
    expect(fileViewFor("/a/doc.pdf", "application/pdf", "base64")).toEqual({
      kind: "other",
      mimeType: "application/pdf",
    });
    expect(fileViewFor("/a/photo.heic", undefined, "base64")).toEqual({
      kind: "other",
      mimeType: null,
    });
  });
});

describe("describeReadError", () => {
  it("names the common failures", () => {
    expect(describeReadError(new Error("ENOENT: no such file or directory"))).toBe(
      "Файла уже нет: его удалили или переместили.",
    );
    expect(describeReadError(new Error("EISDIR: illegal operation"))).toBe("Это папка, а не файл.");
    expect(describeReadError(new Error("Host host_1 is offline"))).toBe(
      "Машина, на которой работал агент, сейчас не в сети.",
    );
    expect(describeReadError(new Error("Path does not exist: /gone.png"))).toBe(
      "Файла уже нет: его удалили или переместили.",
    );
    expect(describeReadError(new Error("Host not found"))).toBe(
      "Машины, на которой работал тред, больше нет в bb.",
    );
    expect(describeReadError(new Error("Host is suspended"))).toBe(
      "Машина, на которой работал агент, сейчас не в сети.",
    );
    expect(describeReadError(new Error("Path does not exist: /data/suspended/a.png"))).toBe(
      "Файла уже нет: его удалили или переместили.",
    );
    expect(
      describeReadError(new Error("File size 12000000 bytes exceeds the 10 MB limit")),
    ).toBe("Файл больше 10 МБ — bb не отдаёт такие для просмотра.");
    expect(describeReadError("boom")).toBe("Не удалось прочитать файл: boom");
  });
});
