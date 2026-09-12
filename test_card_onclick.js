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

let lastInnerHTML = '';
const mockDocument = {
  getElementById: (id) => ({
    appendChild: (child) => { lastInnerHTML = child.innerHTML; },
    classList: { add: ()=>{}, remove: ()=>{} }
  }),
  createElement: () => ({ innerHTML: '' }),
  addEventListener: () => {}
};

const vm = require('vm');
const mockWindow = { addEventListener: () => {}, location: { reload: () => {} } };
const sandbox = {
  window: mockWindow,
  document: mockDocument,
  cachedDrafts: {},
  currentCampaignId: null,
  scrollToBottom: () => {},
  navigator: { clipboard: { writeText: () => {} } },
  localStorage: { getItem: () => null, setItem: () => {} },
  setTimeout: (fn) => fn(),
  console: console
};
sandbox.global = sandbox;
const context = vm.createContext(sandbox);

vm.runInContext(scriptContent, context);

// Test with tricky prompt containing quotes, apostrophes, newlines, angle brackets
const testPrompt = "Women's Shoes & Bags: 20% Off!\nLimited time only; \"bestseller\"";
context.renderDynamicCreativeCard(testPrompt, { copy: 'Hello', hashtags: [] }, null);

// Extract all onclick attributes from lastInnerHTML
const onRegex = /onclick="([^"]*)"/g;
let om;
let count = 0;
while ((om = onRegex.exec(lastInnerHTML)) !== null) {
  count++;
  const jsCode = om[1];
  console.log(`Checking handler #${count}: ${jsCode}`);
  try {
    new Function(jsCode);
    console.log(`  -> Syntax OK!`);
  } catch (err) {
    console.error(`  -> SYNTAX ERROR: ${err.message}`);
    process.exit(1);
  }
}
console.log(`All ${count} dynamic onclick handlers validated with 0 syntax errors!`);

// Test calling regenerateCardDraft
try {
  context.fillAndSend = (text) => { console.log(`fillAndSend called with: ${text}`); };
  const cardId = Object.keys(context.cachedDrafts)[0];
  context.regenerateCardDraft(cardId);
  console.log("regenerateCardDraft executed successfully!");
} catch (e) {
  console.error("Error executing regenerateCardDraft:", e);
  process.exit(1);
}
