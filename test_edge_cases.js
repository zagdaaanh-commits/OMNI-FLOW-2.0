const fs = require('fs');

const html = fs.readFileSync('app/static/index.html', 'utf8');

const scriptRegex = /<script(?![^>]*src)([^>]*)>([\s\S]*?)<\/script>/gi;
let sm;
let scriptContent = "";
while ((sm = scriptRegex.exec(html)) !== null) {
  if (!sm[1].includes('tailwind-config')) {
    scriptContent += "\n" + sm[2];
  }
}

const idRegex = /\bid=["']([^"']+)["']/gi;
const htmlIds = new Set();
let m;
while ((m = idRegex.exec(html)) !== null) {
  htmlIds.add(m[1]);
}

const elements = new Map();

function createMockElement(id = '', tag = 'div') {
  const classList = new Set();
  const children = [];
  const el = {
    id: id,
    tagName: tag.toUpperCase(),
    value: '',
    textContent: '',
    innerHTML: '',
    style: {},
    classList: {
      add: (...cls) => cls.forEach(c => classList.add(c)),
      remove: (...cls) => cls.forEach(c => classList.delete(c)),
      toggle: (c) => classList.has(c) ? classList.delete(c) : classList.add(c),
      contains: (c) => classList.has(c)
    },
    parentElement: null,
    appendChild: (child) => {
      child.parentElement = el;
      children.push(child);
      return child;
    },
    remove: () => {
      if (el.parentElement && el.parentElement.children) {
        const idx = el.parentElement.children.indexOf(el);
        if (idx !== -1) el.parentElement.children.splice(idx, 1);
      }
    },
    querySelectorAll: (sel) => [],
    querySelector: (sel) => null,
    addEventListener: () => {},
    click: () => {},
    focus: () => {}
  };
  return el;
}

for (const id of htmlIds) {
  elements.set(id, createMockElement(id));
}

const domListeners = {};
const mockWindow = {
  addEventListener: (evt, fn) => {
    domListeners[evt] = domListeners[evt] || [];
    domListeners[evt].push(fn);
  },
  location: { reload: () => {} }
};

const mockLocalStorage = {
  _store: {},
  getItem: (k) => mockLocalStorage._store[k] || null,
  setItem: (k, v) => { mockLocalStorage._store[k] = String(v); },
  removeItem: (k) => { delete mockLocalStorage._store[k]; }
};

const mockDocument = {
  documentElement: createMockElement('html', 'html'),
  getElementById: (id) => {
    if (!elements.has(id)) {
      elements.set(id, createMockElement(id));
    }
    return elements.get(id);
  },
  createElement: (tag) => createMockElement('', tag),
  querySelectorAll: (sel) => [],
  querySelector: (sel) => null,
  addEventListener: (evt, fn) => {
    domListeners[evt] = domListeners[evt] || [];
    domListeners[evt].push(fn);
  }
};

let fetchShouldFail = false;
let fetchReturnStatus = 200;
let fetchReturnBody = {};

const mockFetch = async (url, opts = {}) => {
  if (fetchShouldFail) {
    throw new Error("Network connection lost");
  }
  if (fetchReturnStatus >= 400) {
    return {
      ok: false,
      status: fetchReturnStatus,
      json: async () => ({ detail: "Simulated 500 error from API" })
    };
  }
  return {
    ok: true,
    status: 200,
    json: async () => fetchReturnBody
  };
};

const vm = require('vm');
const sandbox = {
  window: mockWindow,
  document: mockDocument,
  localStorage: mockLocalStorage,
  fetch: mockFetch,
  navigator: { clipboard: { writeText: async () => {} } },
  console: console,
  setTimeout: (fn) => fn(),
  clearTimeout: () => {},
  FileReader: class {
    readAsDataURL(file) {
      this.onload({ target: { result: 'data:image/png;base64,mock' } });
    }
  }
};
sandbox.global = sandbox;

const context = vm.createContext(sandbox);
vm.runInContext(scriptContent, context);

async function testEdgeCases() {
  console.log("=== Edge Case Testing ===");

  // 1. SubmitChat with empty input & no image
  try {
    elements.get('chatInput').value = '   ';
    await vm.runInContext("submitChat()", context);
    console.log("[PASS] submitChat with whitespace-only input does not trigger fetch or errors");
  } catch (e) {
    console.error("[FAIL] submitChat whitespace:", e);
  }

  // 2. SubmitChat with network failure
  try {
    elements.get('chatInput').value = 'Test network fail';
    fetchShouldFail = true;
    await vm.runInContext("submitChat()", context);
    console.log("[PASS] submitChat handled network failure gracefully");
  } catch (e) {
    console.error("[FAIL] submitChat network failure:", e);
  } finally {
    fetchShouldFail = false;
  }

  // 3. SubmitChat with HTTP 500
  try {
    elements.get('chatInput').value = 'Test 500 error';
    fetchReturnStatus = 500;
    await vm.runInContext("submitChat()", context);
    console.log("[PASS] submitChat handled HTTP 500 gracefully");
  } catch (e) {
    console.error("[FAIL] submitChat 500 error:", e);
  } finally {
    fetchReturnStatus = 200;
  }

  // 4. renderDynamicCreativeCard with various userPrompt and data shapes
  const trickyPrompts = [
    "Men's leather jacket",
    "Product with \"quotes\" & <tags>",
    "Line 1\nLine 2\nLine 3",
    "",
    null,
    undefined
  ];

  for (const p of trickyPrompts) {
    try {
      sandbox.tempPrompt = p;
      const res = vm.runInContext("renderDynamicCreativeCard(tempPrompt, { copy: 'Great product', hashtags: ['#cool'] }, null)", context);
      console.log(`[PASS] renderDynamicCreativeCard with prompt: ${JSON.stringify(p)}`);
    } catch (e) {
      console.error(`[FAIL] renderDynamicCreativeCard with prompt ${JSON.stringify(p)}:`, e);
    }
  }

  // 5. Check the generated HTML from renderDynamicCreativeCard with tricky prompt!
  // In a real browser, the card's innerHTML will contain the onclick handler!
  // Let's check if the innerHTML would cause a syntax error if parsed as HTML/JS!
  sandbox.tempPrompt = "Women's Fashion & Shoes: 50% Off 'Now'!";
  vm.runInContext("renderDynamicCreativeCard(tempPrompt, { copy: 'Special sale', hashtags: ['#sale'] }, null)", context);
  const msgArea = elements.get('messagesContainer');
  // Get the last added child's innerHTML
  // Check for onclick inside innerHTML
  console.log("Checking onclick generated in card innerHTML...");
}

testEdgeCases();
