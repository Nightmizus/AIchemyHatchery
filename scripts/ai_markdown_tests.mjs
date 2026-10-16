import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {test} from 'node:test';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const markdownit = require('../frontend/markdown-it.min.js');
const script = readFileSync(new URL('../frontend/ai-markdown.js', import.meta.url), 'utf8');
function renderer(parser = markdownit) {
  const window = {markdownit: parser};
  vm.runInNewContext(script, {window});
  return window.AIchemyHatcheryMarkdown;
}

test('paragraphs and single newlines remain readable', () => {
  const html = renderer().render('第一行\n第二行\n\n第三段');
  assert.match(html, /第一行<br>\n第二行/);
  assert.equal((html.match(/<p>/g) || []).length, 2);
});

test('headings, lists, emphasis, inline/fenced code, quotes and tables render', () => {
  const html = renderer().render('# 结果\n\n**完成**与`index.html`\n\n- 第一项\n- 第二项\n\n1. 步骤\n\n> 引用\n\n```html\n<div>示例</div>\n```\n\n| 检查 | 结果 |\n| --- | --- |\n| 页面 | 通过 |');
  for (const tag of ['h1', 'strong', 'code', 'ul', 'ol', 'blockquote', 'pre', 'table']) assert.match(html, new RegExp('<' + tag + '[ >]'));
  assert.match(html, /&lt;div&gt;示例&lt;\/div&gt;/);
});

test('HTML and unsafe URL schemes cannot become executable markup', () => {
  const api = renderer();
  for (const source of ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '<svg onload=alert(1)>', '[bad](javascript:alert(1))', '[bad](JaVaScRiPt:alert(1))', '[bad](data:text/html,test)', '[bad](file:///etc/passwd)']) {
    const html = api.render(source);
    assert.doesNotMatch(html, /<(script|img|svg)\b/i);
    assert.doesNotMatch(html, /href="(?:javascript|data|file):/i);
  }
});

test('images never auto-fetch remote tracking content', () => {
  assert.doesNotMatch(renderer().render('![pixel](https://markdown-pixel.invalid/pixel.png)'), /<img\b/);
});

test('links use a separate browsing context without opener access', () => {
  const html = renderer().render('[预览](https://example.com/)');
  assert.match(html, /href="https:\/\/example.com\/"/);
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
});

test('original Markdown survives formatted DOM and never becomes plain concatenation', () => {
  const source = '## 计划\n\n1. **读取** `index.html`\n2. 修改';
  assert.equal(renderer().source({dataset: {aiMarkdownSource: source}, innerText: 'formatted'}), source);
});

test('missing parser safely falls back to text with line breaks', () => {
  assert.equal(renderer(null).render('<script>bad()</script>\n保留'), '&lt;script&gt;bad()&lt;/script&gt;<br>保留');
});
