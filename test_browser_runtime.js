const fs = require('fs');

const html = fs.readFileSync('app/static/index.html', 'utf8');

// Find all script tags
const scriptRegex = /<script(?![^>]*src)([^>]*)>([\s\S]*?)<\/script>/gi;
let sm;
let scriptContent = "";
while ((sm = scriptRegex.exec(html)) !== null) {
  if (!sm[1].includes('tailwind-config')) {
    scriptContent += "\n" + sm[2];
  }
}

// Find all element IDs in HTML
const idRegex = /\bid=["']([^"']+)["']/gi;
const htmlIds = new Set();
let m;
while ((m = idRegex.exec(html)) !== null) {
  htmlIds.add(m[1]);
}

// Build DOM Mock
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
      if (el.parentElement) {
        const idx = el.parentElement.children ? el.parentElement.children.indexOf(el) : -1;
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

// Prepopulate elements for all IDs found in HTML
for (const id of htmlIds) {
  elements.set(id, createMockElement(id));
}

// Global environment
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
      // create on the fly if needed
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

const mockFetch = async (url, opts = {}) => {
  return {
    ok: true,
    status: 200,
    json: async () => {
      if (url.includes('/campaigns')) return [{ id: 'camp-1', name: 'Test Campaign', platforms: ['meta', 'tiktok'], status: 'active', budget: 1000 }];
      if (url.includes('/content/generate')) return { copy: 'Generated test copy', hashtags: ['#test', '#ad'], drafts: [] };
      if (url.includes('/analytics/report')) return { totals: { impressions: 100, clicks: 10, spend: 50, ctr: '10%' } };
      if (url.includes('/campaign/boost')) return { campaign_name: 'Test Boost', boost_multiplier: '3x' };
      if (url.includes('/publish/tasks')) return [{ id: 'task-1', platform: 'meta', status: 'queued' }];
      if (url.includes('/publish/schedule')) return [{ id: 'task-new', status: 'published' }];
      if (url.includes('/settings/apis/test')) return { active_services: ['meta', 'openai'], latency_ms: 12 };
      if (url.includes('/settings/apis')) return { masked: {}, raw: {} };
      if (url.includes('/demo/seed')) return { drafts_count: 5, tasks_count: 3 };
      if (url.includes('/auth/me')) return { full_name: 'Admin User', email: 'admin@omniflow.ai' };
      if (url.includes('/auth/login')) return { token: 'mock-jwt-token', user: { full_name: 'Admin', email: 'admin@omniflow.ai' } };
      if (url.includes('/integrations/status')) return { meta: { name: 'Meta', status: 'connected' } };
      if (url.includes('/publish/facebook')) return { success: true, post_id: '101728504668130_123', post_url: 'https://facebook.com/101728504668130_123' };
      return {};
    }
  };
};

// Execute script in mock sandbox
const vm = require('vm');
const sandbox = {
  window: mockWindow,
  document: mockDocument,
  localStorage: mockLocalStorage,
  fetch: mockFetch,
  alert: (msg) => { /* mock alert */ },
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

try {
  vm.runInContext(scriptContent, context);
  console.log("Script evaluated successfully without errors!");
} catch (err) {
  console.error("FATAL: Script evaluation threw an error:", err);
  process.exit(1);
}

// Test DOMContentLoaded
console.log("Testing DOMContentLoaded events...");
if (domListeners['DOMContentLoaded']) {
  domListeners['DOMContentLoaded'].forEach(fn => fn());
}
console.log("DOMContentLoaded executed successfully!");

// Now test all core required functions directly in the context
async function runTests() {
  const tests = [
    { name: "handleInputKey (Enter)", code: "handleInputKey({ key: 'Enter', shiftKey: false, preventDefault: () => {} })" },
    { name: "handleInputKey (Shift+Enter)", code: "handleInputKey({ key: 'Enter', shiftKey: true, preventDefault: () => {} })" },
    { name: "handleInputKey (No event arg)", code: "handleInputKey()" },
    { name: "appendThinkingIndicator()", code: "appendThinkingIndicator()" },
    { name: "removeThinkingIndicator()", code: "removeThinkingIndicator()" },
    { name: "loadCampaigns()", code: "loadCampaigns()" },
    { name: "resetToNewChat()", code: "resetToNewChat()" },
    { name: "submitChat() with text", code: "document.getElementById('chatInput').value = 'Test prompt'; submitChat()" },
    { name: "fillAndSend('Hello')", code: "fillAndSend('Hello')" },
    { name: "simulateVoice()", code: "simulateVoice()" },
    { name: "triggerBoostMode()", code: "triggerBoostMode()" },
    { name: "triggerQuickTool('analytics')", code: "triggerQuickTool('analytics')" },
    { name: "openNewCampaignModal()", code: "openNewCampaignModal()" },
    { name: "closeNewCampaignModal()", code: "closeNewCampaignModal()" },
    { name: "openCampaignDetailsModal('camp-1')", code: "openCampaignDetailsModal('camp-1')" },
    { name: "loadMcpSettings()", code: "loadMcpSettings()" },
    { name: "loadSchedulesList()", code: "loadSchedulesList()" },
    { name: "loadIntegrationsStatus()", code: "loadIntegrationsStatus()" },
    { name: "triggerDemoSeed()", code: "triggerDemoSeed()" },
    { name: "toggleTheme()", code: "toggleTheme()" },
    { name: "openAuthModal('login')", code: "openAuthModal('login')" },
    { name: "switchAuthTab('register')", code: "switchAuthTab('register')" },
    { name: "fillDemoCredentials()", code: "fillDemoCredentials()" },
    { name: "publishToFacebook() [no args]", code: "publishToFacebook()" },
    { name: "publishToFacebook(event)", code: "publishToFacebook({ target: { innerText: 'Publish' } })" },
    { name: "publishToFacebook({ caption })", code: "publishToFacebook({ caption: 'Direct test post' })" },
    { name: "publishCurrentCreative()", code: "publishCurrentCreative()" },
    { name: "approveAndPublish()", code: "approveAndPublish()" },
  ];

  for (const t of tests) {
    try {
      const p = vm.runInContext(t.code, context);
      if (p && typeof p.then === 'function') {
        await p;
      }
      console.log(`[PASS] ${t.name}`);
    } catch (err) {
      console.error(`[FAIL] ${t.name}:`, err.message);
    }
  }
}

runTests();
