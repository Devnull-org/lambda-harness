const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

// Exercise the shipped event handlers with a small DOM and controllable HTTP calls.
class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.scrollTop = 0;
    this.scrollHeight = 1000;
    this.clientHeight = 500;
    this.text = "";
    this.dataset = {};
  }
  set textContent(text) {
    this.text = String(text);
    this.children = [];
  }
  get textContent() {
    return this.text + this.children.map((n) => n.textContent).join("");
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.text = "";
    this.children = children;
  }
  get lastElementChild() {
    return this.children.at(-1);
  }
  getBoundingClientRect() {
    return { bottom: this.bottom ?? 1000 };
  }
  scrollIntoView(options) {
    this.scrollCalls = [...(this.scrollCalls || []), options];
  }
  querySelector() {
    return (
      this.children.find((n) => n.tagName === "button") || new Element("button")
    );
  }
  querySelectorAll() {
    return [];
  }
  addEventListener() {}
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
  }
  focus() {}
  select() {
    this.selected = true;
  }
  setSelectionRange(start, end) {
    this.selectionStart = start;
    this.selectionEnd = end;
  }
  click() {
    this.onclick?.();
  }
}

function task(name = "example") {
  return {
    task_id: name,
    stage: "exploration",
    request: "Investigate the parser",
    base_ref: "main",
    base: "abc123",
    messages: [{ text: "First question" }],
    prompt: { actions: ["question", "done"], approval_token: "test" },
  };
}
function snapshot(current = task()) {
  return {
    repository: "/project",
    base: "main",
    backend: "test",
    tasks: [{ task_id: current.task_id, stage: current.stage }],
    task: current,
    job: null,
    can_stop: false,
  };
}
const response = (body) => ({ ok: true, json: async () => body });
const settle = () => new Promise(setImmediate);

for (const [kind, stage, action] of [
  [undefined, "exploration", "question"],
  ["review", "standalone_review", "question"],
  ["feature", "interview", "answer"],
  ["agent_builder", "agent_builder", "answer"],
]) {
  test(`model selection is available for ${stage} conversations`, async () => {
    const current = snapshot({
      ...task(),
      task_kind: kind,
      stage,
      prompt: { actions: [action] },
    });
    let posted;
    const ui = await app({
      fetcher: async (path, options) => {
        if (path.startsWith("/api/models"))
          return response({
            backend: "test",
            catalogs: [
              { backend: "test", models: [{ id: "careful", name: "Careful" }] },
            ],
          });
        if (path === "/api/command") {
          posted = JSON.parse(options.body);
          return response({});
        }
        return response(current);
      },
    });
    ui.run("render()");
    await settle();
    assert.equal(ui.elements.get("reply-model-picker").hidden, false);
    const picker = ui.elements.get("reply-chat-model");
    picker.value = JSON.stringify({ backend: "test", model: "careful" });
    picker.onchange();
    ui.elements.get("message").value = "Explain the alternative";
    await ui.run(`respond(${JSON.stringify(action)})`);
    assert.equal(posted.model, "careful");
    assert.equal(posted.backend, "test");
    current.task.prompt = { actions: [], available_phases: ["interview"] };
    await ui.run(
      'command({command: "run", task_id: selected, phase: "interview"})',
    );
    assert.equal(posted.model, "careful");
  });
}

test("chat model selection survives polling, reaches the next reply, and labels past responses", async () => {
  const current = snapshot({
    ...task(),
    task_kind: "chat",
    stage: "chat",
    messages: [
      {
        role: "chat",
        answer: "First answer",
        generation: {
          backend: "test",
          model: "quick",
          reported_model: "quick-v2",
        },
      },
    ],
    prompt: { actions: ["answer"] },
  });
  let posted;
  const ui = await app({
    fetcher: async (path, options) => {
      if (path.startsWith("/api/models"))
        return response({
          backend: "test",
          catalogs: [
            {
              backend: "test",
              current: "quick",
              models: [
                { id: "quick", name: "Quick" },
                { id: "careful", name: "Careful" },
              ],
            },
          ],
        });
      if (path === "/api/command") {
        posted = JSON.parse(options.body);
        return response({});
      }
      return response(current);
    },
  });
  ui.run("render()");
  await settle();
  const picker = ui.elements.get("reply-chat-model");
  assert.equal(ui.elements.get("reply-model-picker").hidden, false);
  assert.equal(JSON.parse(picker.value).model, "quick");
  assert.match(ui.elements.get("history").textContent, /quick-v2/);
  picker.value = JSON.stringify({ backend: "test", model: "careful" });
  picker.onchange();
  ui.elements.get("message").value = "An unfinished question";
  await ui.run("refresh()");
  assert.equal(JSON.parse(picker.value).model, "careful");
  assert.equal(ui.elements.get("message").value, "An unfinished question");
  await ui.run('respond("answer")');
  assert.equal(posted.model, "careful");
  assert.equal(posted.backend, "test");
  current.job = { running: true, id: "busy", log: [] };
  await ui.run("refresh()");
  assert.equal(picker.disabled, true);
});

for (const kind of ["chat", "explore", "start", "review", "tests"]) {
  test(`new ${kind} tasks can choose a model from an automatically detected provider`, async () => {
    const current = { ...snapshot(), task: null, backend: "auto" };
    let posted;
    const ui = await app({
      fetcher: async (path, options) => {
        if (path.startsWith("/api/models"))
          return response({
            backend: "auto",
            catalogs: [
              {
                backend: "provider-a",
                models: [{ id: "small", name: "Small" }],
              },
              { backend: "provider-b", models: [{ id: "big", name: "Big" }] },
            ],
          });
        if (path === "/api/command") {
          posted = JSON.parse(options.body);
          current.backend = posted.backend;
          current.task = {
            ...task(posted.task_id),
            task_kind: "chat",
            stage: "backend_blocked",
            messages: [],
            prompt: { actions: [] },
          };
          return response({});
        }
        return response(current);
      },
    });
    ui.run("selected = null");
    ui.kind.value = kind;
    ui.elements.get("new-form").onchange();
    await settle();
    const picker = ui.elements.get("new-chat-model");
    assert.match(picker.textContent, /provider-a.*Small.*provider-b.*Big/);
    picker.value = JSON.stringify({ backend: "provider-b", model: "big" });
    picker.onchange();
    ui.elements.get("task-name").value = "New chat";
    ui.elements.get("request").value = "Hello";
    ui.elements.get("new-form").onsubmit({ preventDefault() {} });
    await settle();
    assert.equal(posted.model, "big");
    assert.equal(posted.backend, "provider-b");
    assert.equal(
      JSON.parse(ui.elements.get("reply-chat-model").value).model,
      "big",
    );
  });
}

test("slow or failed model discovery preserves drafts and leaves default chat available", async () => {
  let finish;
  const current = snapshot({
    ...task(),
    task_kind: "chat",
    stage: "chat",
    prompt: { actions: ["answer"] },
  });
  const ui = await app({
    fetcher: async (path) =>
      path.startsWith("/api/models")
        ? new Promise((resolve) => {
            finish = resolve;
          })
        : response(current),
  });
  ui.run("render()");
  ui.elements.get("message").value = "Keep my draft";
  assert.match(ui.elements.get("reply-model-status").textContent, /Detecting/);
  assert.equal(ui.elements.get("send").disabled, false);
  finish(
    response({
      backend: "test",
      catalogs: [{ backend: "test", models: [], error: "Please sign in" }],
    }),
  );
  await settle();
  assert.match(
    ui.elements.get("reply-model-status").textContent,
    /Please sign in/,
  );
  assert.equal(ui.elements.get("message").value, "Keep my draft");
  assert.equal(ui.elements.get("send").disabled, false);
});

test("switching conversations does not apply another chat's model choice", async () => {
  const current = snapshot({
    ...task(),
    task_kind: "chat",
    stage: "chat",
    prompt: { actions: ["answer"] },
  });
  const ui = await app({
    fetcher: async (path) =>
      path.startsWith("/api/models")
        ? response({
            backend: "test",
            catalogs: [
              {
                backend: "test",
                current: "quick",
                models: [
                  { id: "quick", name: "Quick" },
                  { id: "careful", name: "Careful" },
                ],
              },
            ],
          })
        : response(current),
  });
  ui.run("render()");
  await settle();
  const picker = ui.elements.get("reply-chat-model");
  picker.value = JSON.stringify({ backend: "test", model: "careful" });
  picker.onchange();
  ui.run('selected="other";state.task.task_id="other";render()');
  assert.equal(JSON.parse(picker.value).model, "quick");
});

test("phone access shows the private address and mobile navigation closes after choosing work", async () => {
  const current = {
    ...snapshot(),
    phone: { url: "https://laptop.example.ts.net", ready: true },
  };
  const ui = await app({
    mobile: true,
    fetcher: async () => response(current),
  });
  assert.equal(ui.elements.get("project-navigation").open, false);
  assert.equal(ui.elements.get("phone-access").hidden, false);
  assert.equal(ui.elements.get("phone-url").href, current.phone.url);
  ui.elements.get("project-navigation").open = true;
  await ui.run("refresh()");
  assert.equal(ui.elements.get("project-navigation").open, true);
  await ui.elements.get("tasks").children[0].children[0].onclick();
  assert.equal(ui.elements.get("project-navigation").open, false);
  current.phone.ready = false;
  await ui.run("refresh()");
  assert.match(ui.elements.get("phone-status").textContent, /stopped/);
  current.phone = null;
  await ui.run("refresh()");
  assert.equal(ui.elements.get("phone-access").hidden, true);
});

const deleteSubmission = () => ({
  submitter: { value: "delete" },
  preventDefault() {},
});

test("sidebar deletion targets a completed review without disturbing the open conversation", async () => {
  const project = { id: "project-a", name: "Example", repository: "/example" };
  const current = {
    ...snapshot(),
    project,
    tasks: [
      { task_id: "example", stage: "chat" },
      { task_id: "finished-review", stage: "review_complete" },
    ],
  };
  const requests = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/abandon-plan?task=finished-review")
        return response({
          worktree: "/review/worktree",
          confirmation_token: "review-token",
        });
      if (path === "/api/command") {
        requests.push(options);
        current.tasks.pop();
        current.job = {
          id: "deleted",
          running: false,
          log: ["Task deleted"],
          result: { task_id: "finished-review", stage: "abandoned" },
        };
        return response({});
      }
      return response(current);
    },
  });
  ui.elements.get("message").value = "Keep this draft";
  const remove = ui.elements.get("tasks").children[1].children[1];
  assert.equal(remove.ariaLabel, "Delete task finished-review");
  await remove.onclick();
  assert.equal(ui.run("selected"), "example");
  assert.equal(ui.elements.get("delete-dialog").open, true);
  assert.match(
    ui.elements.get("delete-details").textContent,
    /finished-review/,
  );
  assert.equal(requests.length, 0);
  ui.elements.get("delete-name").value = "example";
  await ui.elements.get("delete-form").onsubmit(deleteSubmission());
  assert.equal(requests.length, 0);
  assert.match(ui.elements.get("delete-error").textContent, /task name/);
  assert.equal(ui.elements.get("delete-dialog").open, true);
  ui.elements.get("delete-name").value = "finished-review";
  await ui.elements.get("delete-form").onsubmit(deleteSubmission());
  assert.deepEqual(JSON.parse(requests[0].body), {
    command: "abandon",
    task_id: "finished-review",
    confirmation: "finished-review",
    token: "review-token",
  });
  assert.equal(requests[0].headers["X-Harness-Project"], "project-a");
  assert.equal(ui.elements.get("delete-dialog").open, false);
  assert.equal(ui.run("selected"), "example");
  assert.equal(ui.elements.get("message").value, "Keep this draft");
  assert.equal(ui.elements.get("work").hidden, false);
  assert.equal(ui.elements.get("tasks").children.length, 1);
  await ui.run("refresh()");
  assert.equal(ui.run("selected"), "example");
});

test("deleting the open task returns to new work and cancelling never sends a command", async () => {
  const current = snapshot();
  const requests = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (path.startsWith("/api/abandon-plan"))
        return response({
          worktree: "/example/worktree",
          confirmation_token: "example-token",
        });
      if (path === "/api/command") {
        requests.push(options);
        current.tasks = [];
        current.task = null;
        current.job = {
          id: "deleted",
          running: false,
          log: [],
          result: { task_id: "example", stage: "abandoned" },
        };
        return response({});
      }
      return response(current);
    },
  });
  ui.elements.get("message").value = "An unsent question for this task";
  await ui.elements.get("abandon").onclick();
  ui.elements.get("delete-cancel").onclick();
  assert.equal(ui.elements.get("delete-dialog").open, false);
  assert.equal(requests.length, 0);
  await ui.elements.get("abandon").onclick();
  assert.equal(ui.elements.get("delete-name").value, "");
  ui.elements.get("delete-name").value = "example";
  await ui.elements.get("delete-form").onsubmit(deleteSubmission());
  await settle();
  assert.equal(requests.length, 1);
  assert.equal(ui.run("selected"), null);
  assert.equal(ui.elements.get("message").value, "");
  assert.equal(ui.elements.get("work").hidden, true);
  assert.equal(ui.elements.get("new-panel").hidden, false);
  assert.match(
    ui.elements.get("tasks").textContent,
    /saved conversations will appear/,
  );
});

test("task deletion is unavailable during running work and preserves confirmation on errors", async () => {
  const ui = await app();
  ui.run("state.job = {id:'running', running:true, log:[]}; render()");
  assert.equal(ui.elements.get("tasks").children[0].children[1].disabled, true);
  assert.equal(ui.elements.get("abandon").disabled, true);
  ui.run("state.job = null; render()");
  let commands = 0;
  ui.fetch(async (path) => {
    if (path.startsWith("/api/abandon-plan"))
      return response({
        worktree: "/example/worktree",
        confirmation_token: "old",
      });
    if (path === "/api/command") {
      commands++;
      return {
        ok: false,
        json: async () => ({ error: "Task changed; open deletion again" }),
      };
    }
    return response(snapshot());
  });
  await ui.elements.get("abandon").onclick();
  ui.elements.get("delete-name").value = "example";
  await ui.elements.get("delete-form").onsubmit(deleteSubmission());
  assert.equal(commands, 1);
  assert.equal(ui.elements.get("delete-dialog").open, true);
  assert.match(ui.elements.get("delete-error").textContent, /Task changed/);
  assert.equal(ui.run("selected"), "example");
  ui.run("requestedProject = 'different-project'");
  await ui.elements.get("delete-form").onsubmit(deleteSubmission());
  assert.equal(commands, 1);
  assert.match(ui.elements.get("delete-error").textContent, /project changed/);
});

test("export downloads only selected tasks and keeps the browser project scope", async () => {
  const requests = [];
  const current = {
    ...snapshot(),
    project: { id: "project-a", name: "Example", repository: "/example" },
    tasks: [
      { task_id: "example", stage: "exploration" },
      { task_id: "another", stage: "chat" },
    ],
  };
  const archive = {
    format: "lambda-harness-handoff",
    created: "2026-10-21T00:00:00Z",
    tasks: [],
  };
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/handoff/export") {
        requests.push(options);
        return response(archive);
      }
      return response(current);
    },
  });
  ui.elements.get("export-task").onclick();
  const choices = ui.elements.get("handoff-tasks").children;
  assert.equal(choices[0].children[0].checked, true);
  assert.equal(choices[1].children[0].checked, false);
  await ui.elements.get("download-handoff").onclick();
  assert.deepEqual(JSON.parse(requests[0].body), { tasks: ["example"] });
  assert.equal(requests[0].headers["X-Harness-Project"], "project-a");
  assert.deepEqual(JSON.parse(await ui.downloads[0].text()), archive);
  assert.match(ui.elements.get("handoff-status").textContent, /unchanged/);
  assert.equal(ui.elements.get("close-handoff").disabled, false);
});

test("import previews the file and requires confirmation before restoring paused work", async () => {
  const requests = [];
  const archive = { format: "lambda-harness-handoff", version: 1 };
  const preview = {
    project: "Example",
    created: "2026-10-21",
    memory_versions: 2,
    keep_current_memory: true,
    confirmation: "reviewed",
    tasks: [
      {
        original: "example",
        task_id: "example-import-1",
        stage: "test_review",
        request: "Write tests",
        commits: 1,
        uncommitted: true,
      },
    ],
  };
  let imported = false;
  const ui = await app({
    fetcher: async (path, options) => {
      if (path.startsWith("/api/handoff/")) {
        requests.push([path, JSON.parse(options.body)]);
        if (path.endsWith("preview")) return response(preview);
        imported = true;
        return response({
          tasks: [{ original: "example", task_id: "example-import-1" }],
        });
      }
      if (imported)
        return response(
          snapshot({
            ...task("example-import-1"),
            stage: "handoff_review",
            prompt: { actions: ["continue_handoff"], approval_token: "new" },
          }),
        );
      return response(snapshot());
    },
  });
  ui.elements.get("import-project").onclick();
  assert.equal(ui.elements.get("confirm-handoff").disabled, true);
  ui.elements.get("handoff-file").files = [
    { size: 100, text: async () => JSON.stringify(archive) },
  ];
  await ui.elements.get("handoff-file").onchange();
  assert.equal(requests.length, 1);
  assert.match(
    ui.elements.get("handoff-preview").textContent,
    /example-import-1/,
  );
  assert.match(
    ui.elements.get("handoff-preview").textContent,
    /current project memory stays/,
  );
  assert.equal(ui.elements.get("confirm-handoff").disabled, false);
  await ui.elements.get("confirm-handoff").onclick();
  assert.deepEqual(requests[1], [
    "/api/handoff/import",
    { archive, confirmation: "reviewed" },
  ]);
  assert.equal(ui.elements.get("handoff-dialog").open, false);
  assert.equal(ui.run("selected"), "example-import-1");
  assert.equal(
    ui.elements.get("actions").children[0].textContent,
    "Continue from saved step",
  );
});

test("invalid replacement files clear a previous import preview and cannot be submitted", async () => {
  const ui = await app();
  ui.elements.get("import-project").onclick();
  ui.run("handoffArchive = {}; handoffPreview = {confirmation: 'stale'}");
  ui.elements.get("handoff-file").files = [
    { size: 4, text: async () => "oops" },
  ];
  await ui.elements.get("handoff-file").onchange();
  assert.equal(ui.elements.get("confirm-handoff").disabled, true);
  assert.equal(ui.run("handoffPreview"), null);
  assert.match(ui.elements.get("handoff-error").textContent, /valid JSON/);
});

test("import preserves an unsent message in the previous task", async () => {
  const ui = await app();
  ui.elements.get("message").value = "An unfinished question";
  ui.elements.get("import-project").onclick();
  ui.run("handoffArchive = {}; handoffPreview = {confirmation: 'reviewed'}");
  ui.fetch(async (path) =>
    path === "/api/handoff/import"
      ? response({ tasks: [{ task_id: "new-task" }] })
      : response(snapshot()),
  );
  await ui.elements.get("confirm-handoff").onclick();
  assert.equal(ui.run("selected"), "example");
  assert.equal(ui.elements.get("message").value, "An unfinished question");
  assert.equal(ui.elements.get("handoff-dialog").open, true);
  assert.match(ui.elements.get("handoff-status").textContent, /preserved/);
});

test("a transfer prevents duplicate clicks and shows progress until the server finishes", async () => {
  const ui = await app();
  let finish;
  let count = 0;
  ui.fetch(async (path) => {
    if (path === "/api/handoff/export") {
      count++;
      return new Promise((resolve) => {
        finish = resolve;
      });
    }
    return response(snapshot());
  });
  ui.elements.get("export-task").onclick();
  const pending = ui.elements.get("download-handoff").onclick();
  await settle();
  assert.equal(ui.elements.get("download-handoff").disabled, true);
  assert.equal(ui.elements.get("close-handoff").disabled, true);
  assert.match(ui.elements.get("handoff-status").className, /transfer-busy/);
  assert.equal(ui.beforeUnload(), true);
  await ui.elements.get("download-handoff").onclick();
  assert.equal(count, 1);
  finish({
    ok: false,
    json: async () => ({ error: "Recheck changed files first" }),
  });
  await pending;
  assert.match(ui.elements.get("handoff-error").textContent, /Recheck/);
  assert.equal(ui.elements.get("close-handoff").disabled, false);
});

async function app({
  pathname = "/",
  fetcher: initialFetch,
  mobile = false,
  clipboard,
  audio,
  storage,
} = {}) {
  const elements = new Map();
  const html = readFileSync("src/lambda_harness/web/index.html", "utf8");
  for (const [, id] of html.matchAll(/id="([^"]+)"/g))
    elements.set(id, new Element());
  elements.get("new-form").append(new Element("button"));
  let fetcher = initialFetch || (async () => response(snapshot()));
  const location = { pathname };
  const navigation = [];
  const pageLoads = [];
  location.assign = (path) => pageLoads.push(path);
  const listeners = {};
  const downloads = [];
  const kind = { value: "explore" };
  // Model native form.reset() using the defaults in the shipped HTML.
  const formHtml = html.match(/<form id="new-form">([\s\S]*?)<\/form>/)[1];
  elements.get("new-form").reset = () => {
    kind.value = formHtml
      .match(/<input[^>]*name="kind"[^>]*checked[^>]*>/)[0]
      .match(/value="([^"]*)"/)[1];
    for (const [, tag, attrs, body] of formHtml.matchAll(
      /<(input|textarea|select)\b([^>]*)(?:>([\s\S]*?)<\/\1>|>)/g,
    )) {
      const id = attrs.match(/id="([^"]+)"/)?.[1];
      if (!id) continue;
      const field = elements.get(id);
      field.value =
        tag === "select"
          ? body.match(/<option[^>]*value="([^"]*)"/)?.[1] || ""
          : tag === "textarea"
            ? body
            : attrs.match(/value="([^"]*)"/)?.[1] || "";
      field.checked = /\bchecked\b/.test(attrs);
    }
  };
  let timerId = 0;
  const timers = new Map();
  const context = vm.createContext({
    innerHeight: 800,
    AudioContext: audio,
    localStorage: storage,
    matchMedia: () => ({ matches: mobile }),
    navigator: { clipboard },
    location,
    history: {
      replaceState: (_, __, path) => {
        location.pathname = path;
      },
      pushState: (_, __, path) => {
        location.pathname = path;
        navigation.push(path);
      },
    },
    addEventListener: (name, callback) => {
      listeners[name] = callback;
    },
    document: {
      getElementById: (id) => elements.get(id),
      querySelector: (selector) =>
        selector.startsWith("meta") ? { content: "test-token" } : kind,
      createElement: (tag) => new Element(tag),
      createTextNode: (text) => {
        const node = new Element("#text");
        node.textContent = text;
        return node;
      },
    },
    fetch: (...args) => fetcher(...args),
    AbortController,
    Blob,
    URL: {
      createObjectURL: (blob) => {
        downloads.push(blob);
        return "blob:handoff";
      },
      revokeObjectURL: () => {},
    },
    setTimeout: (callback, delay) => {
      timers.set(++timerId, { callback, delay });
      return timerId;
    },
    clearTimeout: (id) => timers.delete(id),
  });
  vm.runInContext(
    readFileSync("src/lambda_harness/web/vendor/marked.js", "utf8"),
    context,
  );
  vm.runInContext(
    readFileSync("src/lambda_harness/web/app.js", "utf8"),
    context,
  );
  await settle();
  vm.runInContext("selected = 'example'; if (state) render()", context);
  return {
    gesture: (name = "pointerdown") => listeners[name]?.(),
    elements,
    downloads,
    kind,
    timers,
    location,
    navigation,
    pageLoads,
    beforeUnload: () => {
      let blocked = false;
      listeners.beforeunload?.({
        preventDefault: () => {
          blocked = true;
        },
      });
      return blocked;
    },
    back: async (path) => {
      location.pathname = path;
      await listeners.popstate();
    },
    run: (code) => vm.runInContext(code, context),
    fetch: (fn) => {
      fetcher = fn;
    },
  };
}

test("a task exposes its worktree and copies the exact path while work is running", async () => {
  const current = snapshot({
    ...task(),
    worktree: "/saved work/developer's task/worktree",
  });
  current.job = {
    id: "working",
    task_id: "example",
    running: true,
    operation: "respond",
    log: [],
  };
  let copied;
  const ui = await app({
    fetcher: async () => response(current),
    clipboard: { writeText: async (value) => (copied = value) },
  });
  ui.run("render()");
  assert.equal(ui.elements.get("task-worktree").hidden, false);
  assert.equal(ui.elements.get("worktree-path").value, current.task.worktree);
  assert.equal(ui.elements.get("copy-worktree").disabled, false);
  await ui.elements.get("copy-worktree").onclick();
  assert.equal(copied, current.task.worktree);
  await ui.run("refresh()");
  assert.equal(ui.elements.get("worktree-status").textContent, "Path copied.");
});

for (const clipboard of [
  undefined,
  {
    writeText: async () => {
      throw new Error("Clipboard permission denied");
    },
  },
]) {
  test(`worktree copying offers manual selection when the clipboard is ${clipboard ? "denied" : "unavailable"}`, async () => {
    const current = snapshot({ ...task(), worktree: "/task/worktree" });
    const ui = await app({ fetcher: async () => response(current), clipboard });
    ui.run("render()");
    await ui.elements.get("copy-worktree").onclick();
    assert.equal(ui.elements.get("worktree-path").selected, true);
    assert.match(
      ui.elements.get("worktree-status").textContent,
      /copy.*manually/i,
    );
    assert.equal(ui.elements.get("copy-worktree").disabled, false);
    assert.equal(ui.run("connectionError"), null);
  });
}

test("worktree controls follow the selected task and do not carry over stale copy results", async () => {
  const current = snapshot({ ...task(), worktree: "/first/worktree" });
  let finishCopy;
  const ui = await app({
    fetcher: async () => response(current),
    clipboard: {
      writeText: () => new Promise((resolve) => (finishCopy = resolve)),
    },
  });
  ui.run("render()");
  const copying = ui.elements.get("copy-worktree").onclick();
  current.task = {
    ...task("second"),
    task_kind: "review",
    worktree: "/second/worktree",
  };
  ui.run("selected = 'second'");
  await ui.run("refresh()");
  finishCopy();
  await copying;
  assert.equal(ui.elements.get("worktree-path").value, "/second/worktree");
  assert.equal(ui.elements.get("worktree-status").textContent, "");
  assert.equal(ui.elements.get("copy-worktree").disabled, false);
  delete current.task.worktree;
  await ui.run("refresh()");
  assert.equal(ui.elements.get("worktree-path").value, "");
  assert.equal(ui.elements.get("copy-worktree").disabled, true);
  assert.match(ui.elements.get("worktree-status").textContent, /not.*created/i);
  current.task.task_kind = "agent_builder";
  await ui.run("refresh()");
  assert.equal(ui.elements.get("task-worktree").hidden, true);
});

test("review bases stay editable and preserve branch and PR choices across polling", async () => {
  const ui = await app();
  const base = ui.elements.get("base"),
    head = ui.elements.get("review-head");
  ui.kind.value = "review";
  base.value = "release";
  head.value = "feature/parser";
  head.oninput();
  assert.equal(base.disabled, false);
  assert.equal(base.required, true);
  head.value = " https://github.com/example/project/pull/12/ ";
  head.oninput();
  assert.equal(base.disabled, false);
  assert.equal(base.value, "");
  base.value = "stack/base";
  await ui.run("refresh()");
  assert.equal(base.value, "stack/base");
  head.value = "feature/parser";
  head.oninput();
  assert.equal(base.value, "release");
  head.value = "https://github.com/example/project/pull/12";
  head.oninput();
  assert.equal(base.value, "stack/base");
  base.value = "";
  await ui.run("refresh()");
  assert.equal(base.value, "");
  head.value = "https://github.com/example/project/tree/feature";
  head.oninput();
  assert.equal(base.required, true);
  assert.doesNotMatch(ui.elements.get("base-label").textContent, /PR/);
});

test("review tools post only the selected finding and preserve the draft", async () => {
  const finding = {
    id: "DOC-002",
    title: "Expiry",
    severity: "medium",
    location: "docs.md:2",
    impact: "Blocked",
    sequence: [],
    confidence: "confirmed",
  };
  const reviews = {
    documentation_reviewer: { summary: "Reviewed", findings: [finding] },
  };
  const current = snapshot({
    ...task(),
    stage: "standalone_review",
    task_kind: "review",
    messages: [{ reviews }],
    prompt: { reviews, actions: ["question", "done"] },
    review_actions: { token: "pinned-review", github: true, posts: [] },
  });
  const requests = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") {
        requests.push(JSON.parse(options.body));
        return response({ accepted: true });
      }
      return response(current);
    },
  });
  ui.elements.get("message").value = "An unsent follow-up";
  await ui.run(
    "reviewAction('post_review_comment', {role:'documentation_reviewer', finding_id:'DOC-002'})",
  );
  assert.deepEqual(requests[0], {
    command: "review_action",
    task_id: "example",
    action: "post_review_comment",
    token: "pinned-review",
    role: "documentation_reviewer",
    finding_id: "DOC-002",
  });
  assert.equal(ui.elements.get("message").value, "An unsent follow-up");
  await ui.run("reviewAction('check_links')");
  assert.equal(requests[1].action, "check_links");
  assert.match(
    ui.elements.get("actions").textContent,
    /Check documentation links/,
  );
  current.task.review_actions.posts = [
    {
      role: "documentation_reviewer",
      finding_id: "DOC-002",
      url: "https://github.com/example/project/pull/12#discussion_r123",
    },
  ];
  await ui.run("refresh()");
  assert.match(ui.elements.get("history").textContent, /View GitHub comment/);
  assert.doesNotMatch(
    ui.elements.get("history").textContent,
    /Post comment on GitHub/,
  );
  assert.equal(
    ui.run("githubCommentLink('javascript:alert(1)').href"),
    undefined,
  );
});

for (const reviewers of ["documentation", "all"])
  test(`${reviewers} review submits its selection and custom PR base`, async () => {
    const ui = await app();
    const submitted = [];
    ui.fetch(async (path, options) => {
      if (path === "/api/command") {
        submitted.push(JSON.parse(options.body));
        return response({ accepted: true });
      }
      return response(snapshot());
    });
    ui.kind.value = "review";
    ui.elements.get("review-head").value =
      "https://github.com/example/project/pull/12";
    ui.elements.get("review-head").oninput();
    ui.elements.get("base").value = "release";
    ui.elements.get("reviewers").value = reviewers;
    ui.elements.get("task-name").value = "review-docs";
    ui.elements.get("new-form").onsubmit({ preventDefault() {} });
    await settle();
    assert.equal(submitted[0].reviewers, reviewers);
    assert.equal(submitted[0].base, "release");
    const reviews = {
      documentation_reviewer: { summary: "Setup is accurate.", findings: [] },
    };
    ui.fetch(async () =>
      response(
        snapshot({
          ...task("review-docs"),
          task_kind: "review",
          stage: "standalone_review",
          prompt: { reviews, actions: ["question", "done"] },
        }),
      ),
    );
    await ui.run("refresh()");
    assert.match(ui.elements.get("packet").textContent, /Documentation review/);
    assert.doesNotMatch(ui.elements.get("packet").textContent, /Code review/);
    const html = readFileSync("src/lambda_harness/web/index.html", "utf8");
    assert.match(html, new RegExp(`<option value="${reviewers}">`));
  });

test("task names accept spaces and keep the entered name in requests and navigation", async () => {
  const html = readFileSync("src/lambda_harness/web/index.html", "utf8");
  const input = html.match(/<input\s+id="task-name"[^>]*>/)[0];
  const pattern = new RegExp(
    "^(?:" + input.match(/pattern="([^"]+)"/)[1] + ")$",
    "v",
  );
  for (const name of [
    "Review documentation",
    "Review  documentation",
    "legacy-name_1",
    "A",
    "a".repeat(64),
  ])
    assert(pattern.test(name), name);
  for (const name of [
    "",
    " ",
    " Review",
    "Review ",
    "Review\ncode",
    "../Review",
    "a".repeat(65),
  ])
    assert(!pattern.test(name), name);
  const name = "Review documentation";
  const current = snapshot(task(name));
  const submitted = [],
    paths = [];
  const ui = await app({
    fetcher: async (path, options) => {
      paths.push(path);
      if (path === "/api/command") {
        submitted.push(JSON.parse(options.body));
        return response({ accepted: true });
      }
      return response(current);
    },
  });
  ui.kind.value = "start";
  ui.elements.get("task-name").value = name;
  ui.elements.get("base").value = "main";
  ui.elements.get("request").value = "Check documentation";
  ui.elements.get("new-form").onsubmit({ preventDefault() {} });
  await settle();
  assert.equal(submitted[0].task_id, name);
  assert.equal(ui.run("selected"), name);
  assert.equal(ui.elements.get("title").textContent, name);
  assert.match(ui.elements.get("tasks").textContent, /Review documentation/);
  assert(paths.includes("/api/state?task=Review%20documentation"));
});

test("standalone forms submit their own scope without feature-only options", async () => {
  const ui = await app();
  const submitted = [];
  ui.fetch(async (path, options) => {
    if (path === "/api/command") {
      submitted.push(JSON.parse(options.body));
      return response({ accepted: true });
    }
    return response(snapshot());
  });
  ui.kind.value = "review";
  ui.elements.get("new-form").onchange();
  assert.equal(ui.elements.get("review-options").hidden, false);
  assert.equal(ui.elements.get("test-options").hidden, true);
  assert.equal(ui.elements.get("test-criteria").required, false);
  ui.elements.get("task-name").value = "review-parser";
  ui.elements.get("reviewers").value = "both";
  ui.elements.get("review-head").value =
    "https://github.com/example/project/pull/12";
  ui.elements.get("review-head").oninput();
  assert.equal(ui.elements.get("base").disabled, false);
  assert.equal(ui.elements.get("base").required, false);
  assert.equal(ui.elements.get("base").value, "");
  await ui.run("refresh()");
  assert.equal(ui.elements.get("base").value, "");
  ui.elements.get("new-form").onsubmit({ preventDefault() {} });
  await settle();
  assert.equal(submitted[0].command, "review");
  assert.equal(submitted[0].head, "https://github.com/example/project/pull/12");
  assert.equal(submitted[0].reviewers, "both");
  assert.equal(submitted[0].base, "");
  assert(!("walkthrough" in submitted[0]));
  assert(!("criteria" in submitted[0]));
  assert.match(submitted[0].request, /Review this change/);

  ui.kind.value = "tests";
  ui.elements.get("new-form").onchange();
  assert.equal(ui.elements.get("base").disabled, false);
  assert.equal(ui.elements.get("base").required, true);
  assert.equal(ui.elements.get("test-options").hidden, false);
  assert.equal(ui.elements.get("review-head").required, false);
  ui.elements.get("test-packages").value = "library, app";
  ui.elements.get("test-criteria").value =
    "Accept valid input\n\nReject empty input";
  ui.elements.get("test-expectation").value = "green";
  ui.elements.get("request").value = "Cover the parser";
  ui.elements.get("new-form").onsubmit({ preventDefault() {} });
  await settle();
  assert.equal(submitted[1].command, "tests");
  assert.deepEqual(submitted[1].criteria, [
    "Accept valid input",
    "Reject empty input",
  ]);
  assert.deepEqual(submitted[1].packages, ["library", "app"]);
  assert.equal(submitted[1].expectation, "green");
  assert(!("head" in submitted[1]));
  assert(!("walkthrough" in submitted[1]));
});

test("general chat submits a chat task and renders plain conversation without approval controls", async () => {
  const ui = await app();
  let posted;
  ui.fetch(async (path, options) => {
    if (path === "/api/command") {
      posted = JSON.parse(options.body);
      return response({ accepted: true });
    }
    return response(snapshot());
  });
  ui.kind.value = "chat";
  ui.elements.get("new-form").onchange();
  assert.equal(ui.elements.get("review-options").hidden, true);
  assert.equal(ui.elements.get("test-options").hidden, true);
  assert.equal(ui.elements.get("walkthrough-option").hidden, true);
  assert.equal(ui.elements.get("start-task").textContent, "Start chat →");
  ui.elements.get("task-name").value = "conversation";
  ui.elements.get("request").value = "Explain pure functions";
  ui.elements.get("new-form").onsubmit({ preventDefault() {} });
  await settle();
  assert.equal(posted.command, "chat");
  assert.equal(posted.request, "Explain pure functions");
  assert(!("walkthrough" in posted));
  ui.run(`state.task.task_id = "conversation"; state.task.stage = "chat"; state.task.task_kind = "chat";
    state.task.messages = [{role: "chat", answer: "A pure function has no effects", references: []}, {text: "Tell me more"}, {role: "chat", answer: "Results depend on inputs", references: ["Example.hs:3"]}];
    state.task.prompt = {actions: ["answer"], references: ["Example.hs:3"]}; render();`);
  assert.match(
    ui.elements.get("history").textContent,
    /A pure function has no effects.*Tell me more.*Results depend on inputs/s,
  );
  assert.equal(ui.elements.get("packet").hidden, true);
  assert.equal(ui.elements.get("composer").hidden, false);
  assert.equal(ui.elements.get("message-action").hidden, true);
  assert.equal(ui.elements.get("composer-label").textContent, "Message");
  assert.equal(ui.elements.get("actions").children.length, 0);
  assert.match(
    ui.elements.get("exploration-notes").textContent,
    /Example.hs:3/,
  );
});

test("the message action picker only appears for a choice and sends the active action", async () => {
  const current = snapshot({
    ...task(),
    stage: "agent_error",
    prompt: { actions: ["retry"] },
  });
  const posted = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") posted.push(JSON.parse(options.body));
      return response(current);
    },
  });
  const picker = ui.elements.get("message-action");
  assert.equal(picker.hidden, true);
  assert.equal(ui.elements.get("send").disabled, true);

  current.task.stage = "exploration";
  current.task.prompt = { actions: ["question", "done"] };
  await ui.run("refresh()");
  assert.equal(picker.hidden, true);
  assert.equal(picker.value, "question");
  assert.equal(ui.elements.get("send").disabled, false);
  ui.elements.get("message").value = "What are the alternatives?";
  ui.elements.get("message").onkeydown({
    key: "Enter",
    ctrlKey: true,
    preventDefault() {},
  });
  await settle();
  assert.equal(posted[0].action, "question");

  current.task.stage = "specification_review";
  current.task.prompt = { actions: ["approve", "question", "revise"] };
  await ui.run("refresh()");
  assert.equal(picker.hidden, false);
  assert.deepEqual(
    picker.children.map((option) => option.textContent),
    ["Ask a question", "Revise instructions"],
  );
  picker.value = "revise";
  await ui.run("refresh()");
  assert.equal(picker.value, "revise");
  ui.elements.get("message").value = "Keep the existing interface";
  ui.elements.get("send").click();
  await settle();
  assert.equal(posted[1].action, "revise");
});

test("standalone review history shows findings once and keeps the finish action clear", async () => {
  const ui = await app();
  ui.run(`state.task.task_kind = "review";
    state.task.stage = "standalone_review";
    state.task.prompt.reviews = {haskell_reviewer: {summary: "Checked parser boundaries", findings: [{title: "Empty input crashes", severity: "high", location: "Parser.hs:12", impact: "Requests fail", sequence: [], evidence: "Uses head", recommendation: "Handle []", conditions: "Empty input", id: "HASKELL-1", confidence: "confirmed"}]}};
    state.task.messages = [{reviews: state.task.prompt.reviews}];
    render();`);
  assert.match(ui.elements.get("history").textContent, /Empty input crashes/);
  assert.doesNotMatch(
    ui.elements.get("packet").textContent,
    /Empty input crashes/,
  );
  assert.match(ui.elements.get("actions").textContent, /Finish review/);
  assert.doesNotMatch(
    ui.elements.get("actions").textContent,
    /Approve|Fix selected/,
  );
});

test("project inspection shows suites and sources and keeps questions in conversation", async () => {
  const current = snapshot({
    ...task(),
    stage: "validation_setup",
    messages: [
      {
        role: "validation_discovery",
        answer: "Tests use Cabal.",
        questions: ["Which service is needed?"],
      },
    ],
    prompt: {
      actions: ["answer", "retry"],
      questions: ["Which service is needed?"],
    },
    validation_knowledge: {
      summary: "Tests use Cabal.",
      suites: [
        {
          package: "example-app",
          suites: ["unit", "integration"],
          framework: "Tasty",
          execution: "Integration starts a local service.",
        },
      ],
      sources: [
        { path: "example-app/example-app.cabal", reason: "Suite definitions" },
      ],
      checks: null,
    },
  });
  const ui = await app({ fetcher: async () => response(current) });
  assert.match(
    ui.elements.get("history").textContent,
    /Project inspection.*Which service is needed/s,
  );
  const packet = ui.elements.get("packet");
  assert.match(
    packet.textContent,
    /Project test setup.*Tasty.*integration.*Suite definitions/s,
  );
  assert.doesNotMatch(packet.textContent, /Which service is needed/);
  assert.equal(
    packet.children.find((item) => item.tagName === "details").open,
    undefined,
  );
  assert.equal(ui.elements.get("send").disabled, false);
});

test("approved interviews offer explicit existing-test paths and scope recovery", async () => {
  const current = snapshot({
    ...task(),
    stage: "specification_ready",
    prompt: {
      actions: [],
      available_phases: [
        "explore",
        "interview",
        "tests",
        "implement",
        "refactor",
      ],
    },
  });
  let posted;
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") posted = JSON.parse(options.body);
      return response(current);
    },
  });
  const phases = ui.elements.get("phases");
  assert.match(phases.textContent, /Write failing tests/);
  assert.match(phases.textContent, /Implement using existing tests/);
  assert.match(phases.textContent, /Refactor using existing tests/);
  await phases.children
    .find((node) => node.textContent === "Implement using existing tests")
    .onclick();
  assert.equal(posted.command, "run");
  assert.equal(posted.phase, "implement");
  current.task.stage = "backend_blocked";
  current.task.prompt = { actions: ["retry", "choose_phase"] };
  await ui.run("refresh()");
  await ui.elements
    .get("actions")
    .children.find((node) => node.textContent === "Choose another step")
    .onclick();
  assert.equal(posted.command, "respond");
  assert.equal(posted.action, "choose_phase");
});

test("interview responses stay between replies and appear only once", async () => {
  const specification = {
    problem: "Increment the input",
    expected_behavior: "Add one",
    acceptance_criteria: [{ id: "A1", description: "Zero becomes one" }],
    out_of_scope: [],
    constraints: [],
  };
  const current = snapshot({
    ...task(),
    stage: "specification_review",
    messages: [
      { role: "interviewer", questions: ["Which inputs?"], specification },
      { text: "Integers." },
      { role: "interviewer", questions: ["Include negatives?"], specification },
      { text: "Yes." },
      { role: "interviewer", questions: [], specification },
    ],
    prompt: { specification, actions: ["approve", "question", "revise"] },
  });
  const ui = await app({ fetcher: async () => response(current) });
  const history = ui.elements.get("history");
  assert.deepEqual(
    history.children.map((item) => item.textContent.replaceAll("\n", "")),
    [
      "Initial requestInvestigate the parser",
      "InterviewerWhich inputs?",
      "YouIntegers.",
      "InterviewerInclude negatives?",
      "YouYes.",
      "InterviewerIncrement the inputAdd oneAcceptance criteriaA1: Zero becomes one",
    ],
  );
  assert.doesNotMatch(
    ui.elements.get("packet").textContent,
    /Increment the input/,
  );
  current.task.messages.push({
    question: "Why zero?",
    answer: "It is the smallest example.",
  });
  current.task.prompt.answer = "It is the smallest example.";
  await ui.run("refresh()");
  assert.match(history.children.at(-2).textContent, /Why zero/);
  assert.match(history.children.at(-1).textContent, /smallest example/);
  assert.doesNotMatch(
    ui.elements.get("packet").textContent,
    /smallest example/,
  );
});

test("current interviewer questions are not repeated below the conversation", async () => {
  const questions = ["Which input types?"];
  const current = snapshot({
    ...task(),
    stage: "interview",
    messages: [{ role: "interviewer", questions }],
    prompt: { questions, actions: ["answer"] },
  });
  const ui = await app({ fetcher: async () => response(current) });
  assert.match(ui.elements.get("history").textContent, /Which input types/);
  assert.doesNotMatch(
    ui.elements.get("packet").textContent,
    /Which input types/,
  );
});

test("history is visible and updates when only saved messages change", async () => {
  const ui = await app();
  const history = ui.elements.get("history");
  assert(!history.children.some((n) => n.tagName === "details" && !n.open));
  ui.run("state.task.messages.push({text: 'A new follow-up'}); render()");
  assert.match(history.textContent, /A new follow-up/);
});

test("sending shows the question immediately and prevents duplicate submissions", async () => {
  const ui = await app();
  let finishPost;
  let posts = 0;
  ui.fetch(async (path) => {
    if (path === "/api/command") {
      posts++;
      return new Promise((resolve) => {
        finishPost = resolve;
      });
    }
    const saved = snapshot();
    saved.job = { id: "job", task_id: "example", running: true, log: [] };
    return response(saved);
  });
  ui.elements.get("message").value = "What about multiline\ninput?";
  const first = ui.run("respond('question')");
  assert.match(
    ui.elements.get("history").textContent,
    /What about multiline\ninput\?/,
  );
  assert.equal(ui.elements.get("send").disabled, true);
  await ui.run("respond('question')");
  assert.equal(posts, 1);
  finishPost(response({ accepted: true }));
  await first;
  assert.match(ui.elements.get("history").textContent, /What about multiline/);
  ui.run(
    "state.task.messages.push({text: 'What about multiline\\ninput?'}); render()",
  );
  assert.equal(
    ui.elements.get("history").textContent.split("What about multiline").length,
    2,
  );
});

test("a thinking indicator appears while sending and stays until the response finishes", async () => {
  const current = snapshot();
  let accept;
  const ui = await app({
    fetcher: async (path) => {
      if (path.startsWith("/api/models")) return response({ catalogs: [] });
      if (path === "/api/command")
        return new Promise((resolve) => {
          accept = resolve;
        });
      return response(current);
    },
  });
  const indicator = () =>
    ui.elements
      .get("history")
      .children.find((item) => item.className === "message response-progress");
  ui.elements.get("message").value = "What are the tradeoffs?";
  const pending = ui.run("respond('question')");
  assert.match(indicator().textContent, /Sending/);
  assert.equal(indicator().role, "status");
  current.job = {
    id: "working",
    task_id: "example",
    running: true,
    operation: "respond",
    log: [],
  };
  current.can_stop = true;
  accept(response({}));
  await pending;
  assert.match(indicator().textContent, /Thinking/);
  assert.equal(ui.elements.has("activity-spinner"), false);
  current.stopping = true;
  await ui.run("refresh()");
  assert.match(indicator().textContent, /Stopping/);
  current.job.running = false;
  current.stopping = false;
  current.task.messages.push({ role: "chat", answer: "The answer" });
  await ui.run("refresh()");
  assert.equal(indicator(), undefined);
  assert.equal(ui.elements.has("activity-spinner"), false);
  assert.match(ui.elements.get("history").textContent, /The answer/);
});

for (const kind of ["model", "validation"]) {
  test(`stop ${kind} is available beside Send and preserves the unsent draft`, async () => {
    const current = snapshot();
    current.job = {
      id: "working",
      task_id: "example",
      running: true,
      operation: "respond",
      log: [],
    };
    current.can_stop = true;
    current.activity_kind = kind;
    let finishStop;
    let requests = 0;
    const ui = await app({
      fetcher: async (path) => {
        if (path === "/api/stop") {
          requests++;
          return new Promise((resolve) => {
            finishStop = resolve;
          });
        }
        return response(current);
      },
    });
    ui.run("render()");
    const stop = ui.elements.get("composer-stop");
    assert.equal(ui.elements.has("stop"), false);
    assert.equal(stop.hidden, false);
    assert.equal(stop.disabled, false);
    assert.match(
      stop.textContent,
      kind === "model" ? /Stop model/ : /Stop checks/,
    );
    assert.match(
      ui.elements.get("history").textContent,
      /Thinking|Running checks/,
    );
    ui.elements.get("message").value = "Keep my revised instructions";
    const pending = stop.onclick();
    assert.equal(stop.disabled, true);
    assert.equal(stop.textContent, "Stopping…");
    await stop.onclick();
    assert.equal(requests, 1);
    current.can_stop = false;
    current.stopping = true;
    finishStop(response({ stopping: true }));
    await pending;
    assert.equal(stop.disabled, true);
    assert.equal(
      ui.elements.get("message").value,
      "Keep my revised instructions",
    );
    current.job.running = false;
    current.stopping = false;
    await ui.run("refresh()");
    assert.equal(stop.hidden, true);
    assert.equal(ui.elements.get("send").disabled, false);
    assert.equal(
      ui.elements.get("message").value,
      "Keep my revised instructions",
    );
  });
}

test("another task does not show activity or block this conversation", async () => {
  const current = snapshot();
  current.job = {
    id: "other",
    task_id: "other task",
    running: true,
    operation: "respond",
    log: [],
  };
  current.can_stop = true;
  const ui = await app({ fetcher: async () => response(current) });
  ui.run("render()");
  assert.equal(ui.elements.get("composer-stop").hidden, true);
  assert.equal(ui.elements.has("activity"), false);
  assert.equal(ui.elements.has("activity-spinner"), false);
  assert.equal(ui.elements.get("send").disabled, false);
  assert.equal(ui.elements.get("new-task").disabled, false);
});

test("thinking status handles connection loss, other tasks, and backend errors", async () => {
  const current = snapshot();
  current.job = {
    id: "working",
    task_id: "example",
    running: true,
    operation: "respond",
    log: [],
  };
  const ui = await app({ fetcher: async () => response(current) });
  ui.run("render()");
  const indicator = () =>
    ui.elements
      .get("history")
      .children.find((item) => item.className === "message response-progress");
  assert.match(indicator().textContent, /Thinking/);
  ui.fetch(async () => {
    throw new Error("Connection failed");
  });
  await ui.run("refresh()");
  assert.match(indicator().textContent, /Connection lost/);
  assert.notEqual(indicator().children[0].className, "thinking-indicator");
  assert.equal(ui.elements.has("activity-spinner"), false);
  ui.fetch(async () => response(current));
  await ui.run("refresh()");
  assert.match(indicator().textContent, /Thinking/);
  current.task = task("another");
  ui.run('selected = "another"');
  await ui.run("refresh()");
  assert.equal(indicator(), undefined);
  current.task = task();
  current.job.running = false;
  current.job.error = "Usage limit";
  ui.run('selected = "example"');
  await ui.run("refresh()");
  assert.equal(indicator(), undefined);
  assert.equal(ui.elements.has("activity-spinner"), false);
});

test("a failed poll retries and keeps the draft", async () => {
  const ui = await app();
  ui.elements.get("message").value = "Unsent draft";
  ui.fetch(async () => {
    throw new Error("Temporary connection failure");
  });
  await ui.run("refresh()");
  assert.match(ui.elements.get("error").textContent, /retry/i);
  assert.equal(ui.elements.get("message").value, "Unsent draft");
  assert([...ui.timers.values()].some(({ delay }) => delay === 2000));
  ui.fetch(async () => response(snapshot()));
  await ui.run("refresh()");
  assert.equal(ui.elements.get("error").hidden, true);
  assert.equal(ui.elements.get("send").disabled, false);
});

test("late responses cannot replace the task selected more recently", async () => {
  const ui = await app();
  let oldResponse;
  ui.fetch(async (path) =>
    path.includes("task=one")
      ? new Promise((resolve) => {
          oldResponse = resolve;
        })
      : response(snapshot(task("two"))),
  );
  const old = ui.run("selected = 'one'; refresh()");
  await ui.run("selected = 'two'; refresh()");
  oldResponse(response(snapshot(task("one"))));
  await old;
  assert.equal(ui.elements.get("title").textContent, "two");
});

test("a stalled request times out and polling recovers without discarding text", async () => {
  const ui = await app();
  ui.elements.get("message").value = "Keep my draft";
  ui.fetch(
    (path, options) =>
      new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () =>
          reject(new Error("Aborted")),
        );
      }),
  );
  const pending = ui.run("refresh()");
  const deadline = [...ui.timers.values()].find(({ delay }) => delay === 10000);
  assert(deadline);
  deadline.callback();
  await pending;
  assert.match(ui.elements.get("error").textContent, /did not respond/);
  assert.equal(ui.elements.get("message").value, "Keep my draft");
  assert([...ui.timers.values()].some(({ delay }) => delay === 2000));
});

test("an unsuccessful submission leaves the question available for correction", async () => {
  const ui = await app();
  ui.elements.get("message").value = "Keep this follow-up";
  ui.fetch(async () => ({
    ok: false,
    json: async () => ({ error: "Action is not available" }),
  }));
  await assert.rejects(
    ui.run("respond('question')"),
    /Action is not available/,
  );
  assert.equal(ui.elements.get("message").value, "Keep this follow-up");
  assert.match(
    ui.elements.get("history").textContent,
    /Delivery not confirmed/,
  );
  assert.equal(ui.elements.get("send").disabled, false);
});

function theme(saved, blocked = false, legacy = null) {
  const picker = {
    value: "",
    addEventListener: (_, fn) => {
      picker.change = fn;
    },
  };
  const root = { dataset: {} };
  let stored = saved;
  let writtenKey = null;
  vm.runInNewContext(readFileSync("src/lambda_harness/web/theme.js", "utf8"), {
    document: { documentElement: root, querySelectorAll: () => [picker] },
    localStorage: {
      getItem(key) {
        if (blocked) throw new Error("Storage unavailable");
        return key === "haskell-harness-theme" ? legacy : stored;
      },
      setItem(key, value) {
        if (blocked) throw new Error("Storage unavailable");
        writtenKey = key;
        stored = value;
      },
    },
  });
  return { root, picker, saved: () => stored, writtenKey: () => writtenKey };
}

test("appearance survives the product rename and saves under the new name", () => {
  const ui = theme(null, false, "dark");
  assert.equal(ui.root.dataset.theme, "dark");
  assert.equal(theme("light", false, "dark").root.dataset.theme, "light");
  ui.picker.value = "light";
  ui.picker.change();
  assert.equal(ui.writtenKey(), "lambda-harness-theme");
});

test("appearance restores the saved choice and remembers changes", () => {
  const ui = theme("dark");
  assert.equal(ui.root.dataset.theme, "dark");
  assert.equal(ui.picker.value, "dark");
  ui.picker.value = "light";
  ui.picker.change();
  assert.equal(ui.root.dataset.theme, "light");
  assert.equal(ui.saved(), "light");
  ui.picker.value = "system";
  ui.picker.change();
  assert.equal(ui.root.dataset.theme, "system");
  assert.equal(ui.saved(), "system");
});

test("appearance follows the system initially and works without browser storage", () => {
  assert.equal(theme("unknown").root.dataset.theme, "system");
  const ui = theme(null, true);
  assert.equal(ui.root.dataset.theme, "system");
  ui.picker.value = "dark";
  ui.picker.change();
  assert.equal(ui.root.dataset.theme, "dark");
});

function memoryFixture(ui) {
  const requests = [];
  const versions = {
    original: {
      id: "original",
      revision: "abc123",
      created: "2026-10-20",
      notes: "Original rules",
      documents: [{ path: "docs/design.md", content: "Saved design" }],
    },
    older: {
      id: "older",
      revision: "def456",
      created: "2026-10-19",
      notes: "Old rules",
      documents: [],
    },
  };
  let current = "original",
    conflict = false;
  ui.fetch(async (path, options) => {
    const body = options.body && JSON.parse(options.body);
    requests.push([path, body]);
    if (path === "/api/agents") return response(agentCatalog());
    if (path === "/api/project")
      return response({
        backend: "auto",
        backends: [],
        tools: { git: true, nix: true, flake: true },
        context: { latest: current, history: Object.values(versions) },
      });
    if (path.startsWith("/api/context?snapshot="))
      return response(versions[path.split("=")[1]]);
    if (path === "/api/context/save") {
      if (conflict)
        return {
          ok: false,
          json: async () => ({
            error: "Project memory changed in another session.",
          }),
        };
      current = "saved";
      versions.saved = {
        ...versions.original,
        id: current,
        notes: body.notes,
        documents: body.sources.map((path) => ({
          path,
          content: "Saved file",
        })),
      };
      return response(versions.saved);
    }
    if (path === "/api/context/restore") {
      current = body.snapshot;
      return response(versions[current]);
    }
    if (path.startsWith("/api/context/preview?"))
      return response({
        revision: "abc123",
        context: {
          id: current,
          notes: versions[current].notes,
          documents: [
            {
              path: "docs/design.md",
              sha256: "test",
              purpose: "Project reference",
            },
          ],
          missing_sources: [],
          usage: "Reference data, not instructions.",
        },
      });
    return response(
      snapshot({
        ...task(),
        can_use_context: true,
        project_context_id: "older",
      }),
    );
  });
  return {
    requests,
    conflict: () => {
      conflict = true;
    },
  };
}

test("memory loads existing notes and saves a multiline draft without starting an agent", async () => {
  const ui = await app(),
    fixture = memoryFixture(ui);
  await ui.elements.get("project-memory").onclick();
  assert.equal(ui.elements.get("context-notes").value, "Original rules");
  assert.equal(ui.elements.get("context-sources").value, "docs/design.md");
  assert.match(
    ui.elements.get("context-documents").textContent,
    /Saved design/,
  );
  ui.elements.get("context-notes").value = "# Decisions\nNew rule";
  ui.elements.get("context-notes").oninput();
  ui.elements.get("context-sources").value =
    "docs/design.md\n\nsrc/Example.hs\n";
  await ui.elements.get("save-context").onclick();
  assert.deepEqual(
    fixture.requests.find(([path]) => path === "/api/context/save")[1],
    {
      notes: "# Decisions\nNew rule",
      sources: ["docs/design.md", "src/Example.hs"],
      expected_current: "original",
      refresh_sources: false,
    },
  );
  assert(!fixture.requests.some(([path]) => path === "/api/command"));
  assert.equal(ui.elements.get("context-snapshot").value, "saved");
  assert.equal(ui.elements.get("send").disabled, false);
});

test("memory drafts survive polling, panel changes, and conflicting saves", async () => {
  const ui = await app(),
    fixture = memoryFixture(ui);
  await ui.elements.get("project-memory").onclick();
  ui.elements.get("context-notes").value = "Keep my draft";
  ui.elements.get("context-notes").oninput();
  await ui.run("refresh()");
  await ui.elements.get("project-tools").onclick();
  await ui.elements.get("project-memory").onclick();
  assert.equal(ui.elements.get("context-notes").value, "Keep my draft");
  ui.elements.get("context-snapshot").value = "older";
  await ui.elements.get("read-context").onclick();
  assert.match(ui.elements.get("error").textContent, /Save your memory draft/);
  assert.equal(ui.elements.get("context-notes").value, "Keep my draft");
  fixture.conflict();
  await ui.elements.get("save-context").onclick();
  assert.match(
    ui.elements.get("error").textContent,
    /changed in another session/,
  );
  assert.equal(ui.elements.get("context-notes").value, "Keep my draft");
  assert.equal(ui.elements.get("preview-context").disabled, true);
  assert.equal(ui.elements.get("use-context").disabled, true);
  await ui.elements.get("discard-context").onclick();
  assert.equal(ui.elements.get("context-notes").value, "Original rules");
});

test("earlier memory is read only until explicitly restored and previews show stale sources", async () => {
  const ui = await app(),
    fixture = memoryFixture(ui);
  await ui.elements.get("project-memory").onclick();
  ui.elements.get("context-snapshot").value = "older";
  await ui.elements.get("read-context").onclick();
  assert.equal(ui.elements.get("context-notes").value, "Old rules");
  assert.equal(ui.elements.get("context-notes").readOnly, true);
  assert.equal(ui.elements.get("save-context").disabled, true);
  assert.equal(ui.elements.get("restore-context").disabled, false);
  await ui.elements.get("restore-context").onclick();
  assert.deepEqual(
    fixture.requests.find(([path]) => path === "/api/context/restore")[1],
    { snapshot: "older", expected_current: "original" },
  );
  assert.equal(ui.elements.get("context-notes").readOnly, false);
  await ui.elements.get("preview-context").onclick();
  assert.match(ui.elements.get("context-preview").textContent, /Old rules/);
  assert.match(ui.elements.get("context-preview").textContent, /no excerpt/);
  await ui.elements.get("preview-task-context").onclick();
  assert(
    fixture.requests.some(
      ([path]) => path === "/api/context/preview?task=example",
    ),
  );
  assert(!fixture.requests.some(([path]) => path === "/api/context/use"));
});

test("a late preview cannot replace a new memory draft", async () => {
  const ui = await app();
  memoryFixture(ui);
  await ui.elements.get("project-memory").onclick();
  let finish;
  ui.fetch(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const preview = ui.elements.get("preview-context").onclick();
  ui.elements.get("context-notes").value = "New draft";
  ui.elements.get("context-notes").oninput();
  finish(
    response({
      revision: "abc123",
      context: { id: "old", notes: "Old preview", documents: [] },
    }),
  );
  await preview;
  assert.equal(ui.elements.get("context-preview").textContent, "");
  assert.equal(ui.elements.get("context-notes").value, "New draft");
});

test("chat displays the committed memory receipt and opens that exact version", async () => {
  const ui = await app();
  memoryFixture(ui);
  ui.run(
    `state.task.messages = [{role: "chat", answer: "Here is the note.", references: [], memory_save: {status: "saved", note: "Original rules", snapshot: "original"}}]; render();`,
  );
  const history = ui.elements.get("history");
  assert.match(
    history.textContent,
    /Saved to project memory.*Original rules.*Version original/s,
  );
  const receipt = history.children[1].children.at(-1);
  await receipt.children.find((item) => item.tagName === "button").onclick();
  assert.equal(ui.elements.get("memory-panel").hidden, false);
  assert.equal(ui.elements.get("context-snapshot").value, "original");
  assert.equal(ui.elements.get("context-notes").value, "Original rules");
});

test("a failed chat save displays the reason and proposed note without a saved-version link", async () => {
  const ui = await app();
  ui.run(
    `state.task.messages = [{role: "chat", answer: "The note was not saved.", references: [], memory_save: {status: "error", note: "Keep the core pure", error: "Project notes would exceed 32,000 UTF-8 bytes"}}]; render();`,
  );
  const history = ui.elements.get("history");
  assert.match(
    history.textContent,
    /Memory not saved.*Keep the core pure.*32,000/s,
  );
  assert(!history.textContent.includes("Saved to project memory"));
  const receipt = history.children[1].children.at(-1);
  assert(!receipt.children.some((item) => item.tagName === "button"));
});

test("opening a memory receipt preserves an unsaved editor draft", async () => {
  const ui = await app();
  memoryFixture(ui);
  await ui.elements.get("project-memory").onclick();
  ui.elements.get("context-notes").value = "Unfinished edit";
  ui.elements.get("context-notes").oninput();
  ui.run(
    `state.task.messages = [{role: "chat", answer: "Here is the note.", references: [], memory_save: {status: "saved", note: "Old rules", snapshot: "older"}}]; render();`,
  );
  const receipt = ui.elements.get("history").children[1].children.at(-1);
  await receipt.children.find((item) => item.tagName === "button").onclick();
  assert.equal(ui.elements.get("context-notes").value, "Unfinished edit");
  assert.match(ui.elements.get("error").textContent, /Save your memory draft/);
});

const agentProfile = {
  id: "rust-reviewer",
  version: "0123456789abcdef",
  path: "agents/rust-reviewer.json",
  definition: {
    name: "Rust reviewer",
    role: "reviewer",
    avatar: "crab",
    languages: ["Rust"],
    expertise: ["Ownership", "Async cancellation"],
    instructions: "Review lifetimes and resource cleanup.",
  },
  assignment_problem: null,
};
const agentCatalog = () => ({
  roles: { reviewer: "Code reviewer" },
  profiles: [agentProfile],
  selected: {},
  repository: "/catalog",
  writable: true,
});

test("Agents is a separate catalog page with portraits, search, and full instructions", async () => {
  const ui = await app();
  ui.fetch(async (path) =>
    response(path === "/api/agents" ? agentCatalog() : snapshot()),
  );
  await ui.run("openAgents()");
  assert.equal(ui.elements.get("agents-page").hidden, false);
  assert.equal(ui.elements.get("work").hidden, true);
  assert.equal(ui.elements.get("new-panel").hidden, true);
  assert.equal(ui.elements.get("title").textContent, "Agents");
  assert.equal(ui.elements.get("agent-grid").children.length, 1);
  assert.match(ui.elements.get("agent-grid").textContent, /Rust reviewer/);
  const card = ui.elements.get("agent-grid").children[0];
  assert.match(card.children[0].children[0].src, /^data:image\/svg\+xml,/);
  assert.equal(card.children[0].children[0].alt, "Rust reviewer avatar");
  await card.children.at(-1).children[0].onclick();
  assert.equal(ui.elements.get("agent-details").hidden, false);
  assert.match(
    ui.elements.get("agent-details").textContent,
    /Review lifetimes/,
  );
  ui.elements.get("agent-search").value = "Haskell";
  ui.elements.get("agent-search").oninput();
  assert.match(ui.elements.get("agent-grid").textContent, /No agents match/);
});

test("agent portraits are distinct by ID and stable across renames, reloads, and views", async () => {
  const ui = await app();
  const profiles = [agentProfile, { ...agentProfile, id: "second-reviewer" }];
  ui.fetch(async (path) =>
    response(
      path === "/api/agents" ? { ...agentCatalog(), profiles } : snapshot(),
    ),
  );
  await ui.run("openAgents()");
  const cards = ui.elements.get("agent-grid").children;
  const first = cards[0].children[0].children[0].src;
  const second = cards[1].children[0].children[0].src;
  assert.notEqual(first, second);
  await cards[0].children.at(-1).children[0].onclick();
  assert.equal(
    ui.elements.get("agent-details").children[1].children[0].src,
    first,
  );
  profiles[0] = {
    ...agentProfile,
    definition: { ...agentProfile.definition, name: "Renamed reviewer" },
  };
  profiles.reverse();
  await ui.run("loadAgents()");
  const reloaded = ui.elements.get("agent-grid").children;
  assert.equal(reloaded[0].children[0].children[0].src, second);
  assert.equal(reloaded[1].children[0].children[0].src, first);
  const draft = {
    ...task(),
    task_kind: "agent_builder",
    agent_id: agentProfile.id,
    prompt: { actions: ["answer"], definition: profiles[1].definition },
  };
  ui.fetch(async () => response(snapshot(draft)));
  await ui.run("refresh()");
  assert.equal(ui.elements.get("packet").children[0].children[0].src, first);
});

test("Add and Edit start guided chat and preserve instructions during polling", async () => {
  const ui = await app(),
    commands = [];
  ui.fetch(async (path, options) => {
    if (path === "/api/agents") return response(agentCatalog());
    if (path === "/api/command") {
      commands.push(JSON.parse(options.body));
      return response({ accepted: true });
    }
    return response(snapshot());
  });
  await ui.run("openAgents()");
  ui.elements.get("add-agent").onclick();
  ui.elements.get("agent-request").value =
    "Help me define a Rust reviewer\nI need guidance.";
  await ui.run("refresh()");
  assert.match(ui.elements.get("agent-request").value, /I need guidance/);
  ui.elements.get("agent-form").onsubmit({ preventDefault() {} });
  await settle();
  assert.equal(commands[0].command, "agent_builder");
  assert.equal(commands[0].agent_id, null);
  assert.match(commands[0].request, /\nI need guidance/);
  assert.equal(ui.elements.get("agents-page").hidden, true);
  await ui.run("openAgents()");
  await ui.elements
    .get("agent-grid")
    .children[0].children.at(-1)
    .children[1].onclick();
  assert.equal(
    ui.elements.get("agent-create-title").textContent,
    "Edit Rust reviewer",
  );
  ui.elements.get("agent-request").value = "Focus on cancellation.";
  ui.elements.get("agent-form").onsubmit({ preventDefault() {} });
  await settle();
  assert.equal(commands[1].agent_id, "rust-reviewer");
});

test("agent conversations stay in Agents and explicit save carries the approval token", async () => {
  const ui = await app(),
    commands = [];
  const builder = {
    ...task("builder"),
    task_kind: "agent_builder",
    stage: "agent_builder",
    messages: [
      { role: "agent_builder", answer: "I recommend ownership checks." },
    ],
    prompt: {
      actions: ["answer", "save_agent"],
      definition: agentProfile.definition,
      approval_token: "reviewed-draft",
    },
  };
  const data = {
    ...snapshot(builder),
    tasks: [
      {
        task_id: "builder",
        task_kind: "agent_builder",
        display_name: "Rust reviewer",
        stage: "agent_builder",
      },
    ],
  };
  ui.fetch(async (path, options) => {
    if (path === "/api/command") {
      commands.push(JSON.parse(options.body));
      return response({ accepted: true });
    }
    return response(data);
  });
  ui.run("selected = 'builder'");
  await ui.run("refresh()");
  assert.doesNotMatch(ui.elements.get("tasks").textContent, /builder/);
  assert.match(ui.elements.get("agent-drafts").textContent, /Rust reviewer/);
  assert.match(ui.elements.get("history").textContent, /I recommend ownership/);
  assert.match(ui.elements.get("packet").textContent, /Full instructions/);
  const save = ui.elements
    .get("actions")
    .children.find((node) => node.textContent === "Save to repository");
  assert(save);
  await save.onclick();
  assert.equal(commands[0].action, "save_agent");
  assert.equal(commands[0].token, "reviewed-draft");
});

test("bundled catalogs require a checkout before editing and saving source uses the explicit path", async () => {
  const ui = await app(),
    changes = [];
  ui.fetch(async (path, options) => {
    if (path === "/api/agents")
      return response({ ...agentCatalog(), writable: false, repository: null });
    if (path === "/api/agents/source") {
      changes.push(JSON.parse(options.body));
      return response({});
    }
    return response(snapshot());
  });
  await ui.run("openAgents()");
  assert.equal(ui.elements.get("add-agent").disabled, true);
  assert.equal(ui.elements.get("agent-source").open, true);
  ui.elements.get("agent-repository").value = "/team/agent-repo";
  await ui.elements.get("use-agent-repository").onclick();
  assert.deepEqual(changes, [{ repository: "/team/agent-repo" }]);
});

test("a removed agent stays visible as unavailable so its assignment can be reset", async () => {
  const ui = await app(),
    changes = [];
  ui.fetch(async (path, options) => {
    if (path === "/api/agents")
      return response({
        ...agentCatalog(),
        profiles: [],
        selected: { reviewer: "removed" },
      });
    if (path === "/api/agents/assign") {
      changes.push(JSON.parse(options.body));
      return response({ saved: true });
    }
    return response(snapshot());
  });
  await ui.run("openAgents()");
  const select = ui.elements.get("agent-assignments").children[0].children[0];
  assert.equal(select.value, "removed");
  assert.match(select.textContent, /Unavailable agent/);
  select.value = "";
  await select.onchange();
  assert.deepEqual(changes, [{ role: "reviewer", agent_id: null }]);
});

function assertOnlyPage(ui, visible, title) {
  for (const id of [
    "agents-page",
    "memory-panel",
    "project-panel",
    "work",
    "new-panel",
  ])
    assert.equal(
      ui.elements.get(id).hidden,
      id !== visible,
      `${id} on ${title}`,
    );
  assert.equal(ui.elements.get("title").textContent, title);
  for (const id of ["selection", "choose-backend"])
    assert.equal(
      ui.elements.get(id).hidden,
      true,
      `${id} should not appear on ${title}`,
    );
}

test("project pages replace chat and remain separate while work polls in the background", async () => {
  const ui = await app();
  memoryFixture(ui);
  ui.elements.get("message").value = "Keep my unsent question";
  ui.run(
    "state.job = {id:'running', task_id:'example', running:true, log:['Working…'], selection:{available_backends:[], backend_checks:[]}}; render()",
  );
  await ui.elements.get("project-memory").onclick();
  assertOnlyPage(ui, "memory-panel", "Project memory");
  assert.equal(ui.location.pathname, "/project-memory");
  ui.elements.get("context-notes").value = "Unsaved project notes";
  ui.elements.get("context-notes").oninput();
  await ui.run("refresh()");
  assertOnlyPage(ui, "memory-panel", "Project memory");
  await ui.elements.get("project-tools").onclick();
  assertOnlyPage(ui, "project-panel", "Project settings");
  assert.equal(ui.location.pathname, "/project-settings");
  await ui.run("openAgents()");
  assertOnlyPage(ui, "agents-page", "Agents");
  await ui.run("refresh()");
  assertOnlyPage(ui, "agents-page", "Agents");
  await ui.back("/project-memory");
  assertOnlyPage(ui, "memory-panel", "Project memory");
  assert.equal(ui.elements.get("context-notes").value, "Unsaved project notes");
  await ui.elements.get("close-memory").onclick();
  assert.equal(ui.location.pathname, "/");
  assert.equal(ui.elements.get("work").hidden, false);
  assert.equal(ui.elements.get("memory-panel").hidden, true);
  assert.equal(ui.elements.get("message").value, "Keep my unsent question");
  await ui.elements.get("project-tools").onclick();
  await ui.elements.get("tasks").children[0].children[0].onclick();
  assert.equal(ui.elements.get("project-panel").hidden, true);
  assert.equal(ui.elements.get("work").hidden, false);
  assert.equal(ui.elements.get("message").value, "Keep my unsent question");
});

test("all project page URLs load directly and browser history loads the matching page", async () => {
  const project = {
    backend: "auto",
    backends: [],
    tools: { git: true, nix: true, flake: true },
    context: { latest: null, history: [] },
  };
  const fetcher = async (path) =>
    response(
      path === "/api/agents"
        ? agentCatalog()
        : path === "/api/project"
          ? project
          : { ...snapshot(), tasks: [], task: null },
    );
  for (const [path, id, title] of [
    ["/agents", "agents-page", "Agents"],
    ["/project-memory", "memory-panel", "Project memory"],
    ["/project-settings", "project-panel", "Project settings"],
  ]) {
    const ui = await app({ pathname: path, fetcher });
    assertOnlyPage(ui, id, title);
    assert.equal(ui.elements.get("error").hidden, true);
    await ui.back("/project-settings");
    assertOnlyPage(ui, "project-panel", "Project settings");
    await ui.back("/agents");
    assertOnlyPage(ui, "agents-page", "Agents");
    assert.equal(ui.elements.get("agent-grid").children.length, 1);
    await ui.back("/project-memory");
    assertOnlyPage(ui, "memory-panel", "Project memory");
    assert.equal(ui.elements.get("context-notes").value, "");
  }
});

test("project memory and settings identify their owner and use project-specific links", async () => {
  const identity = {
    id: "project-a",
    name: "example-app",
    repository: "/code/example-app",
  };
  const project = {
    project: identity,
    backend: "auto",
    backend_origin: "project",
    backends: [],
    tools: { git: true, nix: true, flake: true },
    context: { latest: null, history: [] },
  };
  const ui = await app({
    fetcher: async (path) =>
      response(
        path === "/api/project"
          ? project
          : path === "/api/agents"
            ? { ...agentCatalog(), project: identity }
            : {
                ...snapshot(),
                project: identity,
                repository: identity.repository,
              },
      ),
  });
  assert.equal(ui.elements.get("project-name").textContent, "example-app");
  assert.equal(
    ui.elements.get("project-memory").href,
    "/projects/project-a/memory",
  );
  assert.equal(
    ui.elements.get("project-tools").href,
    "/projects/project-a/settings",
  );
  await ui.elements.get("project-memory").onclick();
  assert.equal(ui.location.pathname, "/projects/project-a/memory");
  assert.match(ui.elements.get("title").textContent, /example-app.*Memory/);
  assert.match(
    ui.elements.get("memory-project").textContent,
    /\/code\/example-app/,
  );
  await ui.elements.get("project-tools").onclick();
  assert.equal(ui.location.pathname, "/projects/project-a/settings");
  assert.match(ui.elements.get("title").textContent, /example-app.*Settings/);
  assert.match(
    ui.elements.get("settings-project").textContent,
    /\/code\/example-app/,
  );
  assert.match(
    ui.elements.get("backend-preference-status").textContent,
    /Saved for this project/,
  );
  await ui.back("/projects/project-a/memory");
  assert.equal(ui.elements.get("memory-panel").hidden, false);
  assert.equal(ui.elements.get("project-panel").hidden, true);
  await ui.run("openAgents()");
  assert.equal(ui.location.pathname, "/projects/project-a/agents");
  assert.equal(ui.elements.get("title").textContent, "Agents");
  assert.match(
    ui.elements.get("agent-project-title").textContent,
    /example-app/,
  );
});

test("project bookmarks load directly and history changes project through a fresh page", async () => {
  const identity = {
    id: "project-a",
    name: "example-app",
    repository: "/code/example-app",
  };
  const project = {
    project: identity,
    backend: "auto",
    backend_origin: "project",
    backends: [],
    tools: { git: true, nix: true, flake: true },
    context: { latest: null, history: [] },
  };
  const requests = [];
  const fetcher = async (path) => {
    requests.push(path);
    return response(
      path === "/api/project" ? project : { ...snapshot(), project: identity },
    );
  };
  const ui = await app({ pathname: "/projects/project-a/memory", fetcher });
  assert.equal(ui.elements.get("memory-panel").hidden, false);
  assert.match(ui.elements.get("title").textContent, /example-app.*Memory/);
  requests.length = 0;
  await ui.back("/projects/project-b/settings");
  assert.equal(ui.elements.get("project-panel").hidden, true);
  assert.deepEqual(ui.pageLoads, ["/projects/project-b/settings"]);
  assert.deepEqual(requests, []);
  await ui.elements.get("project-tools").onclick();
  assert.equal(ui.location.pathname, "/projects/project-a/settings");
  assert.equal(ui.elements.get("project-panel").hidden, false);
  assert.equal(ui.elements.get("error").hidden, true);
});

test("an empty workspace can register a project and open it from Projects", async () => {
  const project = {
    id: "project-a",
    name: "example",
    repository: "/code/example",
    available: true,
    running: false,
  };
  const projects = [];
  const requests = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/projects") {
        if (options.method === "POST") {
          requests.push(JSON.parse(options.body));
          projects.push(project);
          return response(project);
        }
        return response({ projects });
      }
      return response({
        ...snapshot(),
        project: null,
        projects,
        tasks: [],
        task: null,
      });
    },
  });
  assert.equal(ui.location.pathname, "/projects");
  assert.equal(ui.elements.get("projects-page").hidden, false);
  assert.equal(ui.elements.get("project-navigation").hidden, true);
  assert.equal(ui.elements.get("new-panel").hidden, true);
  ui.elements.get("project-path").value = project.repository;
  await ui.elements.get("project-add-form").onsubmit({ preventDefault() {} });
  assert.deepEqual(requests, [{ repository: project.repository }]);
  assert.equal(ui.elements.get("project-path").value, "");
  assert.match(ui.elements.get("project-list").textContent, /example/);
  await ui.elements
    .get("project-list")
    .children[0].children[1].children[0].onclick();
  assert.deepEqual(ui.pageLoads, ["/projects/project-a"]);
});

test("project selection scopes every request and does not discard drafts silently", async () => {
  const projects = ["a", "b"].map((id) => ({
    id: `project-${id}`,
    name: "same-name",
    repository: `/code/${id}`,
    available: true,
    running: false,
  }));
  const requests = [];
  const ui = await app({
    pathname: "/projects/project-b",
    fetcher: async (path, options) => {
      requests.push({ path, project: options.headers["X-Harness-Project"] });
      return response({ ...snapshot(), project: projects[1], projects });
    },
  });
  assert.equal(ui.elements.get("project-picker").value, "project-b");
  assert.match(ui.elements.get("project-picker").textContent, /\/code\/a/);
  assert.match(ui.elements.get("project-picker").textContent, /\/code\/b/);
  const firstCard = ui.elements.get("project-list").children[0];
  await ui.run("refresh()");
  assert.equal(ui.elements.get("project-list").children[0], firstCard);
  ui.elements.get("message").value = "Keep this draft";
  ui.elements.get("project-picker").value = "project-a";
  await ui.elements.get("project-picker").onchange();
  assert.deepEqual(ui.pageLoads, ["/projects/project-a"]);
  assert.equal(ui.beforeUnload(), true);
  assert.equal(ui.elements.get("message").value, "Keep this draft");
  assert.ok(
    requests.length > 0 &&
      requests.every((request) => request.project === "project-b"),
  );
});

test("project removal is explicit, handles running work, and can remove the last entry", async () => {
  const project = {
    id: "project-a",
    name: "example",
    repository: "/code/example",
    available: true,
    running: false,
  };
  const requests = [];
  let blocked = true;
  const ui = await app({
    pathname: "/projects",
    fetcher: async (path, options) => {
      if (path === "/api/projects/remove") {
        requests.push(JSON.parse(options.body));
        return blocked
          ? {
              ok: false,
              json: async () => ({ error: "This project has running work." }),
            }
          : response({ projects: [] });
      }
      if (path === "/api/projects") return response({ projects: [project] });
      return response({ ...snapshot(), project, projects: [project] });
    },
  });
  ui.elements.get("message").value = "Unsent text";
  await ui.elements
    .get("project-list")
    .children[0].children[1].children[1].onclick();
  assert.equal(requests.length, 0);
  assert.equal(ui.elements.get("remove-project-dialog").open, true);
  assert.equal(ui.elements.get("remove-project-drafts").hidden, false);
  await ui.elements.get("remove-project-confirm").onclick();
  assert.equal(ui.elements.get("remove-project-dialog").open, true);
  assert.match(
    ui.elements.get("remove-project-error").textContent,
    /running work/,
  );
  assert.deepEqual(ui.pageLoads, []);
  blocked = false;
  await ui.elements.get("remove-project-confirm").onclick();
  assert.deepEqual(requests.at(-1), {
    project_id: "project-a",
    confirmation: "project-a",
  });
  assert.equal(ui.elements.get("remove-project-dialog").open, false);
  assert.deepEqual(ui.pageLoads, ["/projects"]);
  assert.equal(ui.beforeUnload(), false);
});

test("blocked code patches expose an implementation continuation with its approval token", async () => {
  const ui = await app();
  const current = {
    ...task(),
    stage: "test_change_review",
    prompt: {
      actions: ["continue_implementation", "question", "revise", "reject"],
      approval_token: "blocked-patch",
      explanation:
        "The patch has not been applied. Continue implementation with tests unchanged.",
      blocked_paths: ["example-app/example-app.cabal"],
    },
  };
  const commands = [];
  ui.fetch(async (path, options) => {
    if (path === "/api/command") {
      commands.push(JSON.parse(options.body));
      return response({ accepted: true });
    }
    return response(snapshot(current));
  });
  ui.run("selected = 'example'");
  await ui.run("refresh()");
  assert.match(
    ui.elements.get("history").textContent,
    /patch has not been applied/,
  );
  assert.match(ui.elements.get("history").textContent, /example-app.cabal/);
  const control = ui.elements
    .get("actions")
    .children.find(
      (node) =>
        node.textContent === "Continue implementation with tests unchanged",
    );
  assert(control);
  await control.onclick();
  assert.equal(commands[0].action, "continue_implementation");
  assert.equal(commands[0].token, "blocked-patch");
});

for (const [action, label] of [
  ["approve_changes", "Approve changes and continue"],
  ["deny_changes", "Deny and revise"],
]) {
  test(`permission UI shows reason and patch and sends ${action} with its token`, async () => {
    const ui = await app();
    const current = {
      ...task(),
      stage: "test_change_review",
      prompt: {
        summary: "Permission requested",
        actions: [
          "approve_changes",
          "question",
          "revise_changes",
          "deny_changes",
        ],
        approval_token: "exact-patch",
        permission_reason: "Add a dependency needed by the new endpoint tests.",
        permission_files: [
          { path: "example-app/example-app.cabal", operation: "write" },
        ],
        diff: "--- a/example-app/example-app.cabal\n+++ b/example-app/example-app.cabal\n+    servant\n",
      },
    };
    const commands = [];
    ui.fetch(async (path, options) => {
      if (path === "/api/command") {
        commands.push(JSON.parse(options.body));
        return response({ accepted: true });
      }
      return response(snapshot(current));
    });
    ui.run("selected = 'example'");
    await ui.run("refresh()");
    assert.match(ui.elements.get("history").textContent, /dependency needed/);
    assert.match(ui.elements.get("history").textContent, /write: example-app/);
    assert.match(ui.elements.get("history").textContent, /\+    servant/);
    const control = ui.elements
      .get("history")
      .children.find((n) => n.className === "message permission-request")
      .children.find((n) => n.className === "button-row")
      .children.find((n) => n.textContent === label);
    assert(control);
    await control.onclick();
    assert.equal(commands[0].action, action);
    assert.equal(commands[0].token, "exact-patch");
    assert(
      ui.elements
        .get("message-action")
        .children.some(
          (n) =>
            n.value === "revise_changes" && n.textContent === "Request changes",
        ),
    );
  });
}

for (const [revision, title, detail] of [
  [{ changed_files: ["src/Server.hs"] }, "Patch updated", "src/Server.hs"],
  [{ changed_files: [] }, "Patch unchanged", "no file changes"],
  [undefined, "Patch proposal received", "returned a patch proposal"],
]) {
  test(`patch revision receipt shows ${title} in conversation with the proposed diff`, async () => {
    const ui = await app();
    const reply = {
      question: "Add comments to the handlers.",
      answer: "Done.",
      proposal_digest: "revised-patch",
      ...(revision ? { patch_revision: revision } : {}),
    };
    const current = {
      ...task(),
      stage: "test_change_review",
      messages: [reply],
      prompt: {
        actions: ["approve_changes", "question"],
        approval_token: "revised-patch",
        answer: reply.answer,
        diff: "+-- GET /config\n",
        ...(revision ? { patch_revision: revision } : {}),
      },
    };
    ui.fetch(async () => response(snapshot(current)));
    ui.run("selected = 'example'");
    await ui.run("refresh()");
    for (const id of ["history"]) {
      const text = ui.elements.get(id).textContent;
      assert(text.includes(title));
      assert(text.includes(detail));
      assert(text.includes("requires approval"));
    }
    assert(ui.elements.get("history").textContent.includes("Done."));
    assert(ui.elements.get("history").textContent.includes("+-- GET /config"));
  });
}

function bellAudio() {
  const sounds = [];
  class Audio {
    state = "suspended";
    currentTime = 1;
    destination = {};
    async resume() {
      this.state = "running";
    }
    createOscillator() {
      return {
        frequency: { setValueAtTime() {} },
        connect() {},
        disconnect() {},
        start() {
          sounds.push("bell");
        },
        stop() {},
      };
    }
    createGain() {
      return {
        gain: {
          setValueAtTime() {},
          linearRampToValueAtTime() {},
          exponentialRampToValueAtTime() {},
        },
        connect() {},
        disconnect() {},
      };
    }
  }
  return { Audio, sounds };
}

test("permission bell waits for interaction and rings once per new request across tasks", async () => {
  const { Audio, sounds } = bellAudio();
  const ui = await app({ audio: Audio });
  const current = snapshot();
  current.tasks.push({
    task_id: "another task",
    stage: "test_change_review",
    permission_request: "patch-one",
  });
  ui.fetch(async () => response(current));
  await ui.run("refresh()");
  assert.equal(sounds.length, 0);
  await ui.gesture();
  assert.equal(sounds.length, 1);
  await ui.run("refresh()");
  await ui.gesture("keydown");
  assert.equal(sounds.length, 1);
  current.job = { running: true, task_id: "another task", log: [] };
  current.tasks[1].permission_request = "patch-two";
  await ui.run("refresh()");
  assert.equal(sounds.length, 1);
  current.job.running = false;
  await ui.run("refresh()");
  assert.equal(sounds.length, 2);
  await ui.run("refresh()");
  assert.equal(sounds.length, 2);
});

test("permission sound can be muted and its preference survives reload", async () => {
  const { Audio, sounds } = bellAudio();
  let saved = "off";
  const storage = {
    getItem: () => saved,
    setItem: (_, value) => {
      saved = value;
    },
  };
  const ui = await app({ audio: Audio, storage });
  assert.equal(ui.elements.get("permission-sound").ariaPressed, "false");
  await ui.gesture();
  const current = snapshot();
  current.tasks[0].permission_request = "muted-patch";
  ui.fetch(async () => response(current));
  await ui.run("refresh()");
  assert.equal(sounds.length, 0);
  ui.elements.get("permission-sound").onclick();
  await settle();
  assert.equal(saved, "on");
  current.tasks[0].permission_request = "new-patch";
  await ui.run("refresh()");
  assert.equal(sounds.length, 1);
  ui.elements.get("permission-sound").onclick();
  assert.equal(saved, "off");
  const reopened = await app({ audio: Audio, storage });
  assert.equal(
    reopened.elements.get("permission-sound").textContent,
    "Permission sound: off",
  );
});

test("blocked audio and storage do not break permission polling", async () => {
  const ui = await app({
    audio: class {
      constructor() {
        throw new Error("Audio blocked");
      }
    },
    storage: {
      getItem() {
        throw new Error("Storage blocked");
      },
      setItem() {
        throw new Error("Storage blocked");
      },
    },
  });
  await ui.gesture();
  const current = snapshot();
  current.tasks[0].permission_request = "patch";
  ui.fetch(async () => response(current));
  await ui.run("refresh()");
  assert.equal(ui.elements.get("error").hidden, true);
  ui.elements.get("permission-sound").onclick();
  assert.equal(ui.elements.get("permission-sound").ariaPressed, "false");
});

test("blocked implementation shows progress and accepts a repair through the same composer", async () => {
  const ui = await app();
  const update = {
    kind: "work_update",
    answer: "Validation failed: build. Describe the next fix to continue.",
    failure_details: [
      { check: "build", output: "Missing module Hydra.CryptoSpec" },
    ],
  };
  const current = {
    ...task(),
    stage: "validation_blocked",
    messages: [update],
    prompt: {
      actions: ["question", "recheck", "revise"],
      answer: update.answer,
    },
  };
  const commands = [];
  ui.fetch(async (path, options) => {
    if (path === "/api/command") {
      commands.push(JSON.parse(options.body));
      return response({ accepted: true });
    }
    return response(snapshot(current));
  });
  await ui.run("refresh()");
  const history = ui.elements.get("history").textContent;
  assert(history.includes("Validation failed: build"));
  assert(history.includes("Missing module Hydra.CryptoSpec"));
  assert.equal(ui.elements.get("message-action").value, "question");
  ui.elements.get("message").value =
    "Restore the missing modules and continue.";
  await ui.run("respond('question')");
  assert.equal(commands[0].action, "question");
  assert.equal(commands[0].text, "Restore the missing modules and continue.");
});

test("blocked validation rings the attention bell once", async () => {
  const { Audio, sounds } = bellAudio();
  const ui = await app({ audio: Audio });
  await ui.gesture();
  const current = snapshot();
  current.tasks[0] = {
    task_id: "example",
    stage: "validation_blocked",
    attention_request: "failed-build",
  };
  ui.fetch(async () => response(current));
  await ui.run("refresh()");
  assert.equal(sounds.length, 1);
  await ui.run("refresh()");
  assert.equal(sounds.length, 1);
});

test("task status distinguishes a saved failure, running work, and success", async () => {
  const current = snapshot({
    ...task(),
    outcome: {
      kind: "error",
      title: "Stopped: action needed",
      detail: "Validation failed: build",
      guidance: "Choose a recovery action.",
    },
  });
  const ui = await app({ fetcher: async () => response(current) });
  assert.match(
    ui.elements.get("task-outcome").textContent,
    /Validation failed: build/,
  );
  assert.equal(ui.elements.get("task-outcome").dataset.kind, "error");
  current.job = {
    id: "run",
    task_id: "example",
    running: true,
    log: ["Checking build"],
  };
  await ui.run("refresh()");
  assert.match(ui.elements.get("task-outcome").textContent, /Working/);
  assert.doesNotMatch(
    ui.elements.get("task-outcome").textContent,
    /Validation failed/,
  );
  current.task.outcome = {
    kind: "success",
    title: "Step complete",
    detail: "Implementation complete and validated.",
  };
  current.job = { ...current.job, running: false, result: current.task };
  await ui.run("refresh()");
  assert.equal(ui.elements.get("task-outcome").dataset.kind, "success");
  assert.match(
    ui.elements.get("task-outcome").textContent,
    /Implementation complete/,
  );
});

test("automatic choice survives polling and reaches phase and recovery commands", async () => {
  const current = snapshot({
    ...task(),
    mode: "phased",
    prompt: { actions: ["repair"], available_phases: ["implement"] },
  });
  const posted = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") {
        posted.push(JSON.parse(options.body));
        return response({});
      }
      return response(current);
    },
  });
  const picker = ui.elements.get("execution-mode");
  picker.value = "full";
  picker.onchange();
  await ui.run("refresh()");
  assert.equal(picker.value, "full");
  await ui.elements.get("phases").children[0].onclick();
  assert.equal(posted[0].automatic, true);
  await ui.run('respond("repair")');
  assert.equal(posted[1].automatic, true);
  picker.value = "phased";
  picker.onchange();
  await ui.run('respond("repair")');
  assert.equal(posted[2].automatic, false);
});

test("saved automatic mode is restored without treating a mode change as a command", async () => {
  const current = snapshot({ ...task(), mode: "full" });
  let commands = 0;
  const ui = await app({
    fetcher: async (path) => {
      if (path === "/api/command") commands++;
      return response(current);
    },
  });
  const picker = ui.elements.get("execution-mode");
  assert.equal(picker.value, "full");
  picker.value = "phased";
  picker.onchange();
  assert.equal(commands, 0);
});

test("recovery displays literal suggested commands and a no-commits action", async () => {
  const current = snapshot({
    ...task(),
    stage: "agent_error",
    prompt: {
      actions: ["retry", "question", "continue_without_commits"],
      approval_token: "saved-commit-error",
      suggested_commands: [
        {
          explanation: "Run this yourself in a terminal.",
          command: 'export GPG_TTY="$(tty)"\nprintf "<literal>"',
        },
      ],
    },
  });
  const posted = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") {
        posted.push(JSON.parse(options.body));
        return response({});
      }
      return response(current);
    },
  });
  assert.match(ui.elements.get("packet").textContent, /Suggested commands/);
  assert.match(ui.elements.get("packet").textContent, /\$\(tty\)/);
  assert.match(ui.elements.get("packet").textContent, /<literal>/);
  assert.equal(posted.length, 0);
  assert.equal(ui.elements.get("composer").hidden, false);
  const action = ui.elements
    .get("actions")
    .children.find((n) => n.textContent === "Continue without commits");
  await action.onclick();
  assert.equal(posted[0].action, "continue_without_commits");
  assert.equal(posted[0].token, "saved-commit-error");
});

test("recovery commands stay visible in conversation history", async () => {
  const current = snapshot({
    ...task(),
    messages: [
      {
        question: "What should I run?",
        answer: "Check the tool version.",
        suggested_commands: [
          {
            explanation: "Inspect the installed tool.",
            command: "tool --version",
          },
        ],
      },
    ],
  });
  const ui = await app({ fetcher: async () => response(current) });
  assert.match(ui.elements.get("history").textContent, /tool --version/);
});

test("test runner can be invoked in the selected worktree", async () => {
  const current = snapshot({
    ...task(),
    stage: "refactor_ready",
    can_run_checks: true,
  });
  let posted;
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") {
        posted = JSON.parse(options.body);
        return response({});
      }
      return response(current);
    },
  });
  assert.equal(ui.elements.get("task-checks").hidden, false);
  await ui.elements.get("task-checks").onclick();
  assert.equal(posted.command, "task_checks");
  assert.equal(posted.task_id, "example");
});

test("test runner shows command effects, missing suites, and individual failures", async () => {
  const current = snapshot({
    ...task(),
    task_kind: "checks",
    stage: "checks_failed",
    check_plan: {
      summary: "Inspect full coverage",
      commands: [
        {
          name: "Format",
          argv: ["nix", "fmt"],
          purpose: "Format files",
          modifies_files: true,
          directory: ".",
          environment: "project",
        },
      ],
      coverage: ["Unit tests"],
      omissions: ["Nightly AWS"],
      sources: ["justfile"],
    },
    check_results: {
      summary: "Build failed",
      commands: [
        {
          name: "Build",
          argv: ["just", "check"],
          exit_code: 1,
          stderr: "compiler error",
        },
      ],
      not_run: [],
      omissions: ["Nightly AWS"],
    },
    prompt: {
      actions: ["run_checks", "question"],
      approval_token: "plan-token",
    },
  });
  let posted;
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") {
        posted = JSON.parse(options.body);
        return response({});
      }
      return response(current);
    },
  });
  assert.match(ui.elements.get("packet").textContent, /rewrites files/);
  assert.match(ui.elements.get("packet").textContent, /Nightly AWS/);
  assert.match(ui.elements.get("packet").textContent, /compiler error/);
  await ui.run('respond("run_checks")');
  assert.equal(posted.token, "plan-token");
});

test("new test-runner tasks need no manually specified package list", async () => {
  let posted;
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") {
        posted = JSON.parse(options.body);
        return response({});
      }
      return response(snapshot());
    },
  });
  ui.kind.value = "checks";
  ui.run("updateNewTask()");
  assert.equal(ui.elements.get("test-packages").required, false);
  assert.equal(ui.elements.get("request").required, false);
  ui.elements.get("task-name").value = "all-checks";
  ui.elements.get("base").value = "main";
  ui.elements.get("new-form").onsubmit({ preventDefault() {} });
  await settle();
  assert.equal(posted.command, "checks");
});

test("conversation task chat sends a contextual question without a workflow action", async () => {
  const current = snapshot({
    ...task(),
    stage: "tests_requested",
    can_task_chat: true,
    prompt: { actions: [], available_phases: ["tests"] },
  });
  let posted;
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") {
        posted = JSON.parse(options.body);
        return response({});
      }
      return response(current);
    },
  });
  assert.equal(ui.elements.get("composer").hidden, false);
  assert.equal(ui.elements.get("send").disabled, false);
  ui.elements.get("message").value = "Suggest a commit message";
  await ui.elements.get("send").onclick();
  assert.equal(posted.command, "task_chat");
  assert.equal(posted.task_id, "example");
  assert.equal(posted.text, "Suggest a commit message");
  assert.equal(posted.action, undefined);
  assert.equal(posted.automatic, undefined);
  assert.equal(ui.elements.get("message").value, "");
  current.job = {
    running: true,
    task_id: "example",
    operation: "task_chat",
    log: [],
  };
  ui.run("render()");
  assert.equal(ui.elements.get("send").disabled, true);
});

test("one conversation keeps the explorer action and restores a saved task model", async () => {
  const current = snapshot({
    ...task(),
    can_task_chat: true,
    model_selection: { backend: "test", model: "careful" },
    prompt: { actions: ["question", "done"] },
    messages: [
      {
        role: "explorer",
        summary: "Research",
        analysis: "What I found",
        generation: {
          backend: "test",
          model: "careful",
          reported_model: "careful-v2",
        },
      },
    ],
  });
  let posted;
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") {
        posted = JSON.parse(options.body);
        return response({});
      }
      return response(current);
    },
  });
  ui.run("render()");
  assert.equal(ui.elements.has("task-chat-message"), false);
  assert.equal(ui.elements.get("composer").hidden, false);
  assert.equal(
    JSON.parse(ui.elements.get("reply-chat-model").value).model,
    "careful",
  );
  assert.match(
    ui.elements.get("history").textContent,
    /Requested: careful.*Reported: careful-v2/,
  );
  ui.elements.get("message").value = "Explain the tradeoffs";
  await ui.elements.get("send").onclick();
  assert.equal(posted.command, "respond");
  assert.equal(posted.action, "question");
  assert.equal(posted.model, "careful");
});

test("an unsent model selection survives a browser reload", async () => {
  const saved = new Map();
  const storage = {
    getItem: (key) => saved.get(key) || null,
    setItem: (key, value) => saved.set(key, value),
  };
  const first = await app({ storage });
  first.run("render()");
  first.elements.get("reply-chat-model").value = JSON.stringify({
    backend: "test",
    model: "careful",
  });
  first.elements.get("reply-chat-model").onchange();
  const second = await app({ storage });
  second.run("render()");
  assert.equal(
    JSON.parse(second.elements.get("reply-chat-model").value).model,
    "careful",
  );
});

test("task switches isolate activity, drafts, completion, model choices and stop requests", async () => {
  const jobs = ["example", "second"].map((task_id) => ({
    id: task_id + "-job",
    task_id,
    running: true,
    operation: "respond",
    log: [task_id + " log"],
  }));
  const posts = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/stop") {
        posts.push(JSON.parse(options.body));
        return response({ stopping: true });
      }
      const id = path.includes("task=second") ? "second" : "example";
      return response({
        ...snapshot(task(id)),
        jobs,
        job: jobs.find((j) => j.task_id === id),
        project_running: jobs.some((j) => j.running),
        can_stop: true,
        tasks: [task("example"), task("second")],
      });
    },
  });
  ui.elements.get("message").value = "Draft for first";
  ui.run(
    'rememberModel(modelScope(), JSON.stringify({backend:"test", model:"first-model"}))',
  );
  await ui.run('selectTask("second"); refresh()');
  assert.equal(ui.elements.get("message").value, "");
  assert.equal(ui.elements.get("log").textContent, "second log");
  ui.elements.get("message").value = "Draft for second";
  ui.run(
    'rememberModel(modelScope(), JSON.stringify({backend:"test", model:"second-model"}))',
  );
  await ui.elements.get("composer-stop").onclick();
  assert.deepEqual(posts, [{ task_id: "second", job_id: "second-job" }]);
  jobs[0].running = false;
  jobs[0].result = task("example");
  await ui.run("refresh()");
  assert.equal(ui.run("selected"), "second");
  assert.equal(ui.elements.get("message").value, "Draft for second");
  await ui.run('selectTask("example"); refresh()');
  assert.equal(ui.elements.get("message").value, "Draft for first");
  assert.equal(ui.elements.has("activity-spinner"), false);
  assert.equal(ui.elements.get("send").disabled, false);
  assert.equal(ui.elements.get("log").textContent, "example log");
  assert.equal(
    JSON.parse(ui.run("modelChoices.get(modelScope())")).model,
    "first-model",
  );
  assert.match(ui.elements.get("tasks").textContent, /second.*Working/s);
});

test("another task can receive a message while the first is running", async () => {
  const current = snapshot();
  current.jobs = [
    { id: "other-job", task_id: "other", running: true, log: [] },
  ];
  current.project_running = true;
  let posted;
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") {
        posted = JSON.parse(options.body);
        return response({ accepted: true });
      }
      return response(current);
    },
  });
  ui.elements.get("message").value = "Explore this task";
  await ui.run('respond("question")');
  assert.equal(posted.task_id, "example");
  assert.equal(posted.text, "Explore this task");
  assert.equal(ui.elements.has("activity"), false);
  assert.equal(ui.elements.get("export-project").disabled, true);
});

test("switching tasks hides stale activity before the new snapshot arrives", async () => {
  const first = {
    ...snapshot(),
    selected_task_id: "example",
    job: {
      id: "first-job",
      task_id: "example",
      running: true,
      log: ["First task log"],
    },
  };
  first.jobs = [
    first.job,
    {
      id: "second-job",
      task_id: "second",
      running: true,
      log: ["Second task log"],
    },
  ];
  const ui = await app({ fetcher: async () => response(first) });
  ui.run('selectTask("second"); showPage("workspace")');
  assert.equal(ui.elements.has("activity"), false);
  assert.equal(ui.elements.get("work").hidden, true);
});

test("a delayed message submission cannot clear another task draft or show its error there", async () => {
  let rejectSend;
  const ui = await app({
    fetcher: async (path) => {
      if (path === "/api/command")
        return new Promise((_, reject) => {
          rejectSend = reject;
        });
      return response(
        snapshot(task(path.includes("task=second") ? "second" : "example")),
      );
    },
  });
  ui.elements.get("message").value = "First task question";
  const sending = ui.run('guard(() => respond("question"))');
  await ui.run('selectTask("second"); refresh()');
  ui.elements.get("message").value = "Second task draft";
  rejectSend(new Error("First task failed to send"));
  await sending;
  assert.equal(ui.elements.get("message").value, "Second task draft");
  assert.equal(ui.elements.get("error").hidden, true);
  await ui.run('selectTask("example"); refresh()');
  assert.equal(ui.elements.get("message").value, "First task question");
  assert.match(ui.elements.get("error").textContent, /First task failed/);
});

function permissionControls(ui) {
  return ui.elements
    .get("history")
    .children.find((n) => n.className === "message permission-request")
    .children.find((n) => n.className === "button-row").children;
}

test("chat permission controls revise without approval and bind approval to the reviewed patch", async () => {
  const current = snapshot({
    ...task(),
    stage: "test_change_review",
    prompt: {
      summary: "Permission requested",
      permission_reason: "Change the public endpoint behavior",
      permission_files: [{ operation: "write", path: "src/Server.hs" }],
      diff: "+new endpoint",
      actions: [
        "approve_changes",
        "deny_changes",
        "revise_changes",
        "question",
      ],
      approval_token: "first-patch",
    },
  });
  const commands = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") {
        commands.push(JSON.parse(options.body));
        return response({ accepted: true });
      }
      return response(current);
    },
  });
  assert.equal(commands.length, 0);
  assert.equal(ui.elements.get("actions").children.length, 0);
  assert.match(
    ui.elements.get("history").textContent,
    /Change the public endpoint behavior/,
  );
  assert.doesNotMatch(
    ui.elements.get("packet").textContent,
    /Change the public endpoint behavior/,
  );
  const approve = permissionControls(ui).find(
    (n) => n.textContent === "Approve changes and continue",
  );
  await permissionControls(ui)
    .find((n) => n.textContent === "Request changes")
    .onclick();
  assert.equal(ui.elements.get("message-action").value, "revise_changes");
  assert.equal(commands.length, 0);
  await permissionControls(ui)
    .find((n) => n.textContent === "Ask a question")
    .onclick();
  assert.equal(ui.elements.get("message-action").value, "question");
  assert.equal(commands.length, 0);
  current.task.prompt = {
    ...current.task.prompt,
    approval_token: "second-patch",
    permission_reason: "Revised request",
  };
  await ui.run("refresh()");
  assert.match(ui.elements.get("history").textContent, /Revised request/);
  assert.doesNotMatch(
    ui.elements.get("history").textContent,
    /Change the public endpoint behavior/,
  );
  await approve.onclick();
  assert.equal(commands[0].token, "first-patch"); // Server rejects stale approvals, never grants the new patch.
  assert.equal(commands[0].task_id, "example");
  current.job = { id: "running", task_id: "example", running: true, log: [] };
  await ui.run("refresh()");
  assert(permissionControls(ui).every((n) => n.disabled));
  assert.equal(
    ui.elements
      .get("history")
      .children.filter((n) => n.className === "message permission-request")
      .length,
    1,
  );
});

test("activity logs remain available below chat without a floating panel or duplicate stop", async () => {
  const current = snapshot();
  current.job = {
    id: "job",
    task_id: "example",
    running: true,
    log: ["Inspecting this task"],
  };
  const ui = await app({ fetcher: async () => response(current) });
  assert.equal(ui.elements.has("activity"), false);
  assert.equal(ui.elements.has("activity-spinner"), false);
  assert.equal(ui.elements.has("stop"), false);
  assert.equal(ui.elements.get("activity-log").hidden, false);
  assert.equal(ui.elements.get("log").textContent, "Inspecting this task");
  assert.equal(
    ui.elements
      .get("history")
      .children.filter((n) => n.className === "message response-progress")
      .length,
    1,
  );
});

function runtimeControls(ui) {
  return ui.elements
    .get("history")
    .children.find((n) => n.className === "message runtime-permission")
    ?.children.find((n) => n.className === "button-row").children;
}

for (const decision of ["approve", "deny"]) {
  test(`runtime ${decision} reaches the pending job without starting another generation`, async () => {
    const current = snapshot();
    current.job = {
      id: "job-one",
      task_id: "example",
      running: true,
      log: [],
      permission: {
        id: "request-one",
        method: "item/commandExecution/requestApproval",
        details: {
          reason: "Read the linked issue",
          command: "gh issue view 2913",
          cwd: "/task",
          networkApprovalContext: { host: "github.com", protocol: "https" },
        },
      },
    };
    const posts = [];
    let finish;
    const ui = await app({
      fetcher: async (path, options) => {
        if (options?.method === "POST") {
          posts.push({ path, body: JSON.parse(options.body) });
          await new Promise((resolve) => {
            finish = resolve;
          });
          delete current.job.permission;
          return response({ accepted: true });
        }
        return response(current);
      },
    });
    assert.match(
      ui.elements.get("history").textContent,
      /Read the linked issue/,
    );
    assert.match(ui.elements.get("history").textContent, /gh issue view 2913/);
    assert.match(ui.elements.get("history").textContent, /github.com/);
    const controls = runtimeControls(ui);
    assert(controls.every((n) => !n.disabled));
    const click = controls[decision === "approve" ? 0 : 1].onclick();
    await settle();
    assert(runtimeControls(ui).every((n) => n.disabled));
    await controls[0].onclick();
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0], {
      path: "/api/approval",
      body: {
        task_id: "example",
        job_id: "job-one",
        request_id: "request-one",
        decision,
      },
    });
    finish();
    await click;
    assert.equal(runtimeControls(ui), undefined);
  });
}

test("native permission bells announce background jobs once without leaking their controls", async () => {
  const { Audio, sounds } = bellAudio();
  const current = snapshot();
  current.jobs = [
    {
      id: "other-job",
      task_id: "other",
      running: true,
      permission_id: "request-one",
    },
  ];
  const ui = await app({
    audio: Audio,
    fetcher: async () => response(current),
  });
  await ui.gesture();
  assert.equal(sounds.length, 1);
  assert.equal(runtimeControls(ui), undefined);
  await ui.run("refresh()");
  assert.equal(sounds.length, 1);
  current.jobs[0].permission_id = "request-two";
  await ui.run("refresh()");
  assert.equal(sounds.length, 2);
  current.job = {
    ...current.jobs[0],
    task_id: "example",
    permission: {
      id: "request-three",
      method: "item/permissions/requestApproval",
      details: {
        permissions: { network: { enabled: true } },
        reason: "Read sources",
      },
    },
  };
  current.stopping = true;
  await ui.run("refresh()");
  assert(runtimeControls(ui).every((n) => n.disabled));
});

for (const mobile of [false, true]) {
  test(`conversation follows page position and Latest message scrolls the response into view (${mobile ? "mobile" : "desktop"})`, async () => {
    const current = snapshot();
    const ui = await app({ mobile, fetcher: async () => response(current) });
    const history = ui.elements.get("history");
    assert.equal(history.lastElementChild.scrollCalls.length, 1);

    // Reading older content must not jump to a newly arriving reply.
    history.bottom = 1200;
    current.task.messages.push({ text: "Another question" });
    await ui.run("refresh()");
    assert.equal(history.lastElementChild.scrollCalls, undefined);
    ui.elements.get("latest-message").onclick();
    assert.equal(history.lastElementChild.scrollCalls.length, 1);
    assert.equal(history.lastElementChild.scrollCalls[0].block, "end");
    assert.equal(history.scrollTop, 0);

    // Follow new replies only while the conversation end is visible.
    history.bottom = 790;
    current.task.messages.push({ text: "Visible response" });
    await ui.run("refresh()");
    assert.equal(history.lastElementChild.scrollCalls.length, 1);
    history.bottom = -200;
    current.task.messages.push({
      text: "Reply while reading below the conversation",
    });
    await ui.run("refresh()");
    assert.equal(history.lastElementChild.scrollCalls, undefined);

    // Background task updates must not scroll another project page.
    await ui.run('showPage("memory")');
    history.bottom = 790;
    current.task.messages.push({ text: "Background reply" });
    await ui.run("refresh()");
    assert.equal(history.lastElementChild.scrollCalls, undefined);
  });
}

test("Arrow Up restores the latest human message without sending or overwriting a draft", async () => {
  const current = snapshot();
  current.task.messages = [
    { text: "Earlier message" },
    { question: "My latest\nmultiline question", answer: "Assistant response" },
    { answer: "More assistant output" },
  ];
  const posts = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (options?.method === "POST") posts.push(path);
      return response(current);
    },
  });
  const field = ui.elements.get("message");
  let prevented = 0;
  const press = (extra = {}) =>
    field.onkeydown({
      key: "ArrowUp",
      preventDefault() {
        prevented++;
      },
      ...extra,
    });
  press();
  assert.equal(field.value, "My latest\nmultiline question");
  assert.equal(field.selectionStart, field.value.length);
  assert.equal(field.selectionEnd, field.value.length);
  assert.equal(prevented, 1);
  assert.deepEqual(posts, []);
  field.value = "Unsent draft\nsecond line";
  press();
  assert.equal(field.value, "Unsent draft\nsecond line");
  assert.equal(prevented, 1);
  field.value = "";
  for (const modifier of [
    "ctrlKey",
    "metaKey",
    "altKey",
    "shiftKey",
    "isComposing",
  ])
    press({ [modifier]: true });
  assert.equal(field.value, "");
  assert.equal(prevented, 1);
});

test("Arrow Up uses the selected task and can recall pending or initial messages", async () => {
  let current = snapshot();
  const ui = await app({ fetcher: async () => response(current) });
  const field = ui.elements.get("message");
  const press = () => field.onkeydown({ key: "ArrowUp", preventDefault() {} });
  await ui.run('selectTask("other")');
  press();
  assert.equal(field.value, ""); // The previous task is still in the response snapshot.
  current = snapshot({
    ...task("other"),
    messages: [],
    request: "Other task's initial request",
  });
  await ui.run("refresh()");
  press();
  assert.equal(field.value, "Other task's initial request");
  field.value = "";
  await ui.run('pendingMessage = {task_id: "other", text: "Just sent"}');
  press();
  assert.equal(field.value, "Just sent");
  await ui.run('selectTask("example"); selectTask("other")');
  assert.equal(field.value, "Just sent");
});

function copyControl(block) {
  return block.children[0].children.find((node) => node.tagName === "button");
}
function copyStatus(block) {
  return block.children[0].children.find((node) => node.role === "status");
}

test("fenced code has a dedicated block and copies exact code without labels or fences", async () => {
  const copies = [];
  const ui = await app({
    clipboard: { writeText: async (text) => copies.push(text) },
  });
  const source = 'main = do\n  putStrLn "<script>not HTML</script>"\n\n';
  const content = ui.run(
    `prose(${JSON.stringify("Example:\n```haskell\n" + source + "```\nThen run:\n~~~sh\ncabal test\n~~~\nDone.")})`,
  );
  const blocks = content.children.filter(
    (node) => node.className === "code-block",
  );
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].children[0].children[0].textContent, "haskell");
  assert.equal(blocks[0].children[1].tagName, "pre");
  assert.equal(blocks[0].children[1].children[0].tagName, "code");
  assert.equal(blocks[0].children[1].textContent, source);
  await copyControl(blocks[0]).onclick();
  await copyControl(blocks[1]).onclick();
  assert.deepEqual(copies, [source, "cabal test\n"]);
  assert.equal(copyStatus(blocks[0]).textContent, "Copied");
  assert.equal(copyControl(blocks[0]).disabled, false);
});

test("longer fences retain embedded backticks and unfinished blocks retain their last line", async () => {
  const ui = await app();
  const source = "```sh\necho hello\n```\n";
  const content = ui.run(
    `prose(${JSON.stringify("````markdown\n" + source + "````\n```haskell\nvalue = 1")})`,
  );
  const blocks = content.children.filter(
    (node) => node.className === "code-block",
  );
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].children[1].textContent, source);
  assert.equal(blocks[1].children[1].textContent, "value = 1");
});

for (const clipboard of [
  undefined,
  {
    writeText: async () => {
      throw new Error("Denied");
    },
  },
]) {
  test(`code remains available for manual copying when clipboard is ${clipboard ? "denied" : "missing"}`, async () => {
    const ui = await app({ clipboard });
    const block = ui.run('codeBlock("example code", "text")');
    await copyControl(block).onclick();
    assert.match(copyStatus(block).textContent, /copy it manually/);
    assert.equal(block.children[1].textContent, "example code");
    assert.equal(copyControl(block).disabled, false);
    assert.equal(ui.elements.get("error").hidden, true);
  });
}

test("diffs keep their colors and copy their exact patch; suggested commands use code blocks", async () => {
  let copied;
  const ui = await app({
    clipboard: {
      writeText: async (text) => {
        copied = text;
      },
    },
  });
  const diff = "--- a/Test.hs\n+++ b/Test.hs\n@@ -1 +1 @@\n-old\n+new\n";
  const block = ui.run(`diffView(${JSON.stringify(diff)})`);
  const view = block.children[1];
  assert.equal(view.className, "diff-view");
  assert(
    view.children.some((node) => node.className === "diff-line diff-added"),
  );
  assert(
    view.children.some((node) => node.className === "diff-line diff-removed"),
  );
  await copyControl(block).onclick();
  assert.equal(copied, diff);
  const commands = ui.run(
    '(() => { const parent = el("div"); renderSuggestedCommands(parent, [{explanation:"Run tests",command:"just test"}]); return parent; })()',
  );
  const command = commands.children[0].children.find(
    (node) => node.className === "code-block",
  );
  await copyControl(command).onclick();
  assert.equal(copied, "just test");
});

test("Tokyo Day is a persistent explicit light choice alongside Paper and Night", () => {
  const ui = theme("tokyo-day");
  assert.equal(ui.root.dataset.theme, "tokyo-day");
  assert.equal(ui.picker.value, "tokyo-day");
  for (const choice of ["dark", "light", "system", "tokyo-day"]) {
    ui.picker.value = choice;
    ui.picker.change();
    assert.equal(ui.root.dataset.theme, choice);
    assert.equal(ui.saved(), choice);
  }
  const blocked = theme(null, true);
  blocked.picker.value = "tokyo-day";
  blocked.picker.change();
  assert.equal(blocked.root.dataset.theme, "tokyo-day");
});

function descendants(node, tag) {
  return node.children.flatMap((child) => [
    ...(child.tagName === tag ? [child] : []),
    ...descendants(child, tag),
  ]);
}

test("agent replies render bold and inline code in the user's example", async () => {
  const ui = await app();
  const source =
    "**Keep `allowed-assets` separate.** It describes which assets may be present, whereas `l2-minting` controls creating or burning assets inside the Head. Together, your example can permit transfers and admission of allowed existing assets while prohibiting all L2 minting. Do not map `allowed-assets` directly to `AllowOnlyPolicies`: that constructor permits minting under those policies.";
  const content = ui.run(`prose(${JSON.stringify(source)})`);
  assert.equal(
    descendants(content, "strong")[0].textContent,
    "Keep allowed-assets separate.",
  );
  assert.deepEqual(
    descendants(content, "code").map((n) => n.textContent),
    ["allowed-assets", "l2-minting", "allowed-assets", "AllowOnlyPolicies"],
  );
  assert.equal(
    content.textContent,
    source.replaceAll("**", "").replaceAll("`", ""),
  );
});

test("Markdown supports headings, nested lists, quotes, tables, and reference links", async () => {
  const ui = await app();
  const source =
    "## Plan\n\n3. *Explore*\n   - **Inspect**\n4. Test\n\n> ~~Old~~ New\n\n| Name | Result |\n| :--- | ---: |\n| `check` | pass |\n\n[Docs][ref]\n\n[ref]: https://example.com/docs\n\n- [x] Done\n- [ ] Next";
  const content = ui.run(`prose(${JSON.stringify(source)})`);
  assert.equal(descendants(content, "h3")[0].textContent, "Plan");
  assert.equal(descendants(content, "ol")[0].start, 3);
  assert.equal(descendants(content, "em")[0].textContent, "Explore");
  assert.equal(descendants(content, "strong")[0].textContent, "Inspect");
  assert.equal(descendants(content, "blockquote")[0].textContent, "Old New");
  assert.equal(descendants(content, "del")[0].textContent, "Old");
  assert.equal(descendants(content, "table").length, 1);
  assert.equal(descendants(content, "td")[1].className, "align-right");
  assert.equal(descendants(content, "a")[0].href, "https://example.com/docs");
  const checks = descendants(content, "input");
  assert.deepEqual(
    checks.map((n) => n.checked),
    [true, false],
  );
  assert(checks.every((n) => n.disabled));
});

test("Markdown keeps HTML inert, rejects executable links, and does not load remote images", async () => {
  const ui = await app();
  const source =
    "<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[bad](javascript:alert%281%29) [bad](data:text/html,test) [good](https://example.com)\n\n![image](https://example.com/tracker.png)\n\n`**literal** <b>`";
  const content = ui.run(`prose(${JSON.stringify(source)})`);
  assert.equal(descendants(content, "script").length, 0);
  assert.equal(descendants(content, "img").length, 0);
  assert.match(content.textContent, /<script>alert\(1\)<\/script>/);
  const links = descendants(content, "a");
  assert.deepEqual(
    links.map((n) => n.href),
    ["https://example.com", "https://example.com/tracker.png"],
  );
  assert(links.every((n) => n.rel === "noopener noreferrer"));
  assert.equal(descendants(content, "code")[0].textContent, "**literal** <b>");
});

test("Markdown escapes, unfinished emphasis, and code markers remain readable", async () => {
  const ui = await app();
  const content = ui.run(
    `prose(${JSON.stringify("\\*literal\\* **unfinished\n\n``a ` b``")})`,
  );
  assert.match(content.textContent, /\*literal\* \*\*unfinished/);
  assert.equal(descendants(content, "strong").length, 0);
  assert.equal(descendants(content, "code")[0].textContent, "a ` b");
});

test("empty fences do not add a newline and nested fences preserve code for Copy", async () => {
  const ui = await app();
  const content = ui.run(
    `prose(${JSON.stringify("```sh\n```\n\n- Example\n\n  ```sh\n  echo hello\n  ```")})`,
  );
  assert.deepEqual(
    descendants(content, "pre").map((n) => n.textContent),
    ["", "echo hello\n"],
  );
});

test("New task clears old PR review details and cached bases without polling away a new draft", async () => {
  const current = snapshot();
  current.base = "develop";
  const ui = await app({ fetcher: async () => response(current) });
  const field = (id) => ui.elements.get(id);
  ui.kind.value = "review";
  field("task-name").value = "old-review";
  field("request").value = "Review the old PR";
  field("reviewers").value = "documentation";
  field("base").value = "old-branch-base";
  field("review-head").value = "https://github.com/example/project/pull/12";
  field("review-head").oninput();
  field("base").value = "old-pr-base";
  field("test-packages").value = "old-package";
  field("test-criteria").value = "Old behavior";
  field("test-expectation").value = "green";
  field("require-walkthrough").checked = true;

  await field("new-task").onclick();
  assert.equal(ui.kind.value, "explore");
  for (const id of [
    "task-name",
    "request",
    "review-head",
    "test-packages",
    "test-criteria",
  ])
    assert.equal(field(id).value, "", id);
  assert.equal(field("reviewers").value, "both");
  assert.equal(field("test-expectation").value, "red");
  assert.equal(field("require-walkthrough").checked, false);
  assert.equal(field("base").value, "develop");
  assert.equal(field("base").required, true);
  assert.equal(field("review-options").hidden, true);
  assert.equal(field("review-head").required, false);
  assert.equal(field("request").required, true);

  ui.kind.value = "review";
  field("review-head").value = "https://github.com/example/project/pull/34";
  field("review-head").oninput();
  assert.equal(field("base").value, "");
  assert.equal(field("base").required, false);
  field("task-name").value = "new-review";
  field("request").value = "Keep this new draft";
  field("base").value = "new-pr-base";
  await ui.run("refresh()");
  assert.equal(field("task-name").value, "new-review");
  assert.equal(field("request").value, "Keep this new draft");
  assert.equal(field("base").value, "new-pr-base");
  field("review-head").value = "feature/new";
  field("review-head").oninput();
  assert.equal(field("base").value, "develop");
});

function reviewFinding(overrides = {}) {
  return {
    id: "CODE-1",
    severity: "high",
    location: "src/Duration.hs:20",
    title: "Removing Integral breaks callers",
    conditions: "Build with tests",
    sequence: [
      "The change removes Integral Duration.",
      "A caller still requires Integral.",
    ],
    impact: "Tests cannot compile.",
    evidence: "Test.hs:12 uses fromIntegral.",
    confidence: "confirmed",
    recommendation: "Migrate callers.",
    needs_test: false,
    ...overrides,
  };
}

test("overlapping reviewer reports share one card while preserving distinct caller evidence and actions", async () => {
  const code = reviewFinding();
  const security = reviewFinding({
    id: "SEC-1",
    severity: "medium",
    title: "Remaining truncate callers fail",
    evidence: "Cluster.hs:44 uses truncate.",
    impact: "Cluster cannot compile.",
  });
  const reviews = {
    haskell_reviewer: { summary: "Checked correctness.", findings: [code] },
    security_reviewer: {
      summary: "Checked trust boundaries.",
      findings: [security],
    },
  };
  const current = snapshot({
    ...task(),
    task_kind: "review",
    stage: "standalone_review",
    prompt: { reviews, actions: ["question"] },
    review_actions: { github: true, token: "review-token", posts: [] },
  });
  const posted = [];
  const ui = await app({
    fetcher: async (path, options) => {
      if (path === "/api/command") posted.push(JSON.parse(options.body));
      return response(current);
    },
  });
  const content = ui.run(
    `(() => { const p = el("div"); renderReviews(p, ${JSON.stringify(reviews)}); return p; })()`,
  );
  const cards = descendants(content, "article");
  assert.equal(cards.length, 1);
  assert.match(
    cards[0].textContent,
    /Code review · CODE-1 \+ Security review · SEC-1/,
  );
  assert.match(cards[0].textContent, /Test.hs:12/);
  assert.match(cards[0].textContent, /Cluster.hs:44/);
  const related = cards[0].children.find(
    (n) =>
      n.tagName === "details" &&
      n.children[0].textContent.startsWith("Related report"),
  );
  assert(related);
  assert.equal(related.open, undefined);
  await descendants(related, "button")[0].onclick();
  assert.equal(posted[0].role, "security_reviewer");
  assert.equal(posted[0].finding_id, "SEC-1");
  assert.deepEqual(current.task.prompt.reviews, reviews);
});

test("exact duplicate findings and summaries render once with both reviewer attributions and existing receipts", async () => {
  const finding = reviewFinding();
  const reviews = {
    haskell_reviewer: { summary: "Same assessment.", findings: [finding] },
    security_reviewer: {
      summary: "Same assessment.",
      findings: [{ ...finding, id: "SEC-1" }],
    },
  };
  const url = "https://github.com/example/project/pull/12#discussion_r123";
  const ui = await app({
    fetcher: async () =>
      response(
        snapshot({
          ...task(),
          prompt: { reviews, actions: ["question"] },
          review_actions: {
            github: true,
            token: "token",
            posts: [{ role: "security_reviewer", finding_id: "SEC-1", url }],
          },
        }),
      ),
  });
  const content = ui.run(
    `(() => { const p = el("div"); renderReviews(p, ${JSON.stringify(reviews)}); return p; })()`,
  );
  assert.equal(content.textContent.split("Same assessment.").length - 1, 1);
  assert.equal(content.textContent.split(finding.impact).length - 1, 1);
  assert.equal(descendants(content, "article").length, 1);
  assert.equal(descendants(content, "button").length, 0);
  assert.equal(descendants(content, "a")[0].href, url);
  assert.match(content.textContent, /Security review · SEC-1/);
});

test("review grouping keeps separate causes, locations, and same-reviewer findings separate", async () => {
  const ui = await app();
  for (const other of [
    reviewFinding({ id: "SEC-1", sequence: ["Authorization is missing."] }),
    reviewFinding({ id: "SEC-1", location: "Other.hs:20" }),
    reviewFinding({ id: "SEC-1", sequence: [] }),
  ]) {
    const reviews = {
      haskell_reviewer: { findings: [reviewFinding()] },
      security_reviewer: { findings: [other] },
    };
    assert.equal(
      ui.run(`groupedReviewFindings(${JSON.stringify(reviews)}).length`),
      2,
    );
  }
  assert.equal(
    ui.run(
      `groupedReviewFindings(${JSON.stringify({ haskell_reviewer: { findings: [reviewFinding(), reviewFinding({ id: "CODE-2" })] } })}).length`,
    ),
    2,
  );
});

test("identical reviewer follow-up answers display once without changing stored role conversations", async () => {
  const answers = {
    haskell_reviewer: { answer: "Same explanation." },
    security_reviewer: { answer: "Same explanation." },
  };
  const current = snapshot({
    ...task(),
    messages: [
      {
        question: "Why?",
        answer: "Old duplicated combined answer",
        answers_by_role: answers,
      },
    ],
  });
  const ui = await app({ fetcher: async () => response(current) });
  const text = ui.elements.get("history").textContent;
  assert.equal(text.split("Same explanation.").length - 1, 1);
  assert.match(text, /Code review \+ Security review/);
  assert.doesNotMatch(text, /Old duplicated/);
  assert.deepEqual(current.task.messages[0].answers_by_role, answers);
});

test("reviewer answers with different code indentation are not deduplicated", async () => {
  const ui = await app();
  const answers = {
    haskell_reviewer: { answer: "```python\nif ready:\n  run()\n```" },
    security_reviewer: { answer: "```python\nif ready:\nrun()\n```" },
  };
  const content = ui.run(
    `(() => { const p = el("div"); renderReviewerAnswers(p, ${JSON.stringify(answers)}); return p; })()`,
  );
  assert.equal(descendants(content, "pre").length, 2);
});
