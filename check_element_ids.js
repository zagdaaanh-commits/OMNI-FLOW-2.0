const fs = require('fs');
const html = fs.readFileSync('app/static/index.html', 'utf8');

// Find all IDs defined in HTML
const idRegex = /\bid=["']([^"']+)["']/gi;
const definedIds = new Set();
let m;
while ((m = idRegex.exec(html)) !== null) {
  definedIds.add(m[1]);
}

console.log("Total defined element IDs in HTML:", definedIds.size);

// Extract the script content
const scriptRegex = /<script(?![^>]*src)([^>]*)>([\s\S]*?)<\/script>/gi;
let sm;
let scriptContent = "";
while ((sm = scriptRegex.exec(html)) !== null) {
  if (!sm[1].includes('tailwind-config')) {
    scriptContent += "\n" + sm[2];
  }
}

// Find all getElementById in script
const getElemRegex = /document\.getElementById\(\s*["']([^"']+)["']\s*\)/g;
const referencedIds = new Set();
while ((m = getElemRegex.exec(scriptContent)) !== null) {
  referencedIds.add(m[1]);
}

console.log("Total getElementById calls with static string:", referencedIds.size);

const missingIds = [];
for (const id of referencedIds) {
  if (!definedIds.has(id)) {
    missingIds.push(id);
  }
}

console.log("Referenced IDs missing from HTML:", missingIds);
