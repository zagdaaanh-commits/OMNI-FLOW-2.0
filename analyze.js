const fs = require('fs');
const html = fs.readFileSync('app/static/index.html', 'utf8');

// Find all event handlers
const handlerRegex = /\s(on[a-z]+)=([\"\'])(.*?)\2/gis;
let match;
const handlers = [];
while ((match = handlerRegex.exec(html)) !== null) {
  handlers.push({ attr: match[1], code: match[3] });
}

console.log('Total event handlers found:', handlers.length);
handlers.forEach(h => {
  console.log(h.attr + ' -> ' + h.code.trim());
});
