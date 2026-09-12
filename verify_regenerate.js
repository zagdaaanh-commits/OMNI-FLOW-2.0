const fs = require('fs');
const html = fs.readFileSync('app/static/index.html', 'utf8');
const scriptRegex = /<script(?![^>]*src)([^>]*)>([\s\S]*?)<\/script>/gi;
let sm, scriptContent = '';
while ((sm = scriptRegex.exec(html)) !== null) {
  if (!sm[1].includes('tailwind-config')) scriptContent += '\n' + sm[2];
}
const vm = require('vm');
const context = vm.createContext({
  document: {
    getElementById: () => ({ appendChild: ()=>{}, classList: { add: ()=>{}, remove: ()=>{} } }),
    createElement: () => ({ innerHTML: '' }),
    addEventListener: () => {}
  },
  scrollToBottom: () => {},
  navigator: { clipboard: { writeText: () => {} } },
  console: console
});
vm.runInContext(scriptContent, context);
vm.runInContext(`
  renderDynamicCreativeCard("Women's Running Shoes", { copy: 'Check out these kicks', hashtags: ['#shoes'] }, null);
  const keys = Object.keys(cachedDrafts);
  console.log('Total cards in cachedDrafts:', keys.length);
  const cardId = keys[0];
  console.log('Card ID:', cardId);
  console.log('Prompt in cachedDrafts:', cachedDrafts[cardId].prompt);
  fillAndSend = (text) => console.log('fillAndSend called with:', text);
  regenerateCardDraft(cardId);
`, context);
