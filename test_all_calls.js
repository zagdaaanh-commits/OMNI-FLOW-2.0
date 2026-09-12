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

// Extract defined functions and variables
const definedIdentifiers = new Set();

// Function declarations: function foo( or async function foo(
const funcRegex = /(?:async\s+)?function\s+([a-zA-Z0-9_$]+)\s*\(/g;
let m;
while ((m = funcRegex.exec(scriptContent)) !== null) {
  definedIdentifiers.add(m[1]);
}

// Variable declarations: var/let/const foo =
const varRegex = /(?:var|let|const)\s+([a-zA-Z0-9_$]+)\s*=/g;
while ((m = varRegex.exec(scriptContent)) !== null) {
  definedIdentifiers.add(m[1]);
}

console.log("Total defined identifiers:", definedIdentifiers.size);

// 2. Find all onclick/on* attributes in the entire HTML (including inside template strings)
const onAttrRegex = /on[a-z]+\s*=\s*(["'])([\s\S]*?)\1/gi;
const calledFuncs = [];
while ((m = onAttrRegex.exec(html)) !== null) {
  const code = m[2];
  // extract all function-like calls: foo(
  const callRegex = /([a-zA-Z0-9_$]+)\s*\(/g;
  let cm;
  while ((cm = callRegex.exec(code)) !== null) {
    calledFuncs.push({ func: cm[1], code: code.trim() });
  }
}

console.log("Total function call expressions in on* attributes:", calledFuncs.length);

const standardBuiltins = new Set([
  'alert', 'confirm', 'prompt', 'escapeHtml', 'parseInt', 'parseFloat', 'String', 'Number',
  'Boolean', 'Date', 'Math', 'RegExp', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'
]);

const missing = [];
for (const item of calledFuncs) {
  const f = item.func;
  if (!definedIdentifiers.has(f) && !standardBuiltins.has(f)) {
    missing.push(item);
  }
}

if (missing.length > 0) {
  console.log("MISSING FUNCTIONS CALLED IN EVENT HANDLERS:");
  missing.forEach(m => console.log(`- ${m.func} in code: ${m.code}`));
} else {
  console.log("ALL functions called in on* handlers are defined!");
}
