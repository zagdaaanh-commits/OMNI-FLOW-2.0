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

fs.writeFileSync('extracted_script.js', scriptContent, 'utf8');
console.log("Extracted script length:", scriptContent.length);
