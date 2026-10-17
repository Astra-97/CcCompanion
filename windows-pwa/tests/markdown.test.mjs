import assert from 'node:assert/strict';
import test from 'node:test';
import { parseInline, parseMarkdown, safeLinkHref } from '../src/markdown.js';

test('fenced code blocks keep payload verbatim, including markup and sticker tokens', () => {
  const blocks = parseMarkdown('前文\n\n```js\nconst a = "<script>alert(1)</script>";\n// [bqb:爱]\n```\n\n后文');
  assert.deepEqual(blocks.map(({ type }) => type), ['paragraph', 'code', 'paragraph']);
  assert.equal(blocks[1].lang, 'js');
  assert.equal(blocks[1].text, 'const a = "<script>alert(1)</script>";\n// [bqb:爱]');
  assert.equal(blocks[2].inlines[0][0].value, '后文');
});

test('unclosed fence still yields a code block instead of swallowing the parser', () => {
  const blocks = parseMarkdown('```\n直到结尾');
  assert.deepEqual(blocks, [{ type: 'code', lang: '', text: '直到结尾' }]);
});

test('inline code, bold, italic and links parse; emphasis markers inside code stay literal', () => {
  assert.deepEqual(parseInline('用 `**不是粗体**` 和 **真粗体** 与 *斜体*'), [
    { type: 'text', value: '用 ' },
    { type: 'code', value: '**不是粗体**' },
    { type: 'text', value: ' 和 ' },
    { type: 'bold', value: '真粗体' },
    { type: 'text', value: ' 与 ' },
    { type: 'italic', value: '斜体' },
  ]);
  assert.deepEqual(parseInline('[文档](https://example.com/a?b=1)'), [{ type: 'link', href: 'https://example.com/a?b=1', value: '文档' }]);
});

test('unsafe link targets degrade to literal text, never anchors', () => {
  for (const href of ['javascript:alert(1)', 'data:text/html,<script>', 'vbscript:x', 'file:///etc/passwd', 'https://ok.example/evil"x', 'https://ok.example/a b']) {
    assert.equal(safeLinkHref(href), '', href);
  }
  assert.equal(safeLinkHref('https://ok.example/x'), 'https://ok.example/x');
  assert.equal(safeLinkHref('http://ok.example'), 'http://ok.example');
  const tokens = parseInline('[点我](javascript:alert(1))');
  assert.deepEqual(tokens, [{ type: 'text', value: '[点我](javascript:alert(1))' }]);
});

test('raw HTML in source text is inert data for the renderer', () => {
  const tokens = parseInline('<img src=x onerror=alert(1)> <script>alert(2)</script>');
  assert.equal(tokens.length, 1);
  assert.equal(tokens[0].type, 'text');
  assert.match(tokens[0].value, /<script>/);
});

test('lists group consecutive items and respect ordered markers', () => {
  const blocks = parseMarkdown('- 甲\n- 乙\n\n1. 一\n2. 二\n\n普通段落');
  assert.deepEqual(blocks.map(({ type }) => type), ['list', 'list', 'paragraph']);
  assert.equal(blocks[0].ordered, false);
  assert.equal(blocks[0].items.length, 2);
  assert.equal(blocks[1].ordered, true);
  assert.deepEqual(blocks[1].items[1], [{ type: 'text', value: '二' }]);
});

test('paragraph single newlines become explicit breaks; blank lines split paragraphs', () => {
  const blocks = parseMarkdown('第一行\n第二行\n\n新段落');
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].inlines.length, 2, 'two lines kept as break-separated inline runs');
  assert.equal(blocks[0].inlines[1][0].value, '第二行');
});

test('mixed markdown around a full document stays in source order', () => {
  const blocks = parseMarkdown('标题行\n\n- 列表\n\n```py\nprint(1)\n```\n\n收尾 **粗**');
  assert.deepEqual(blocks.map(({ type }) => type), ['paragraph', 'list', 'code', 'paragraph']);
  assert.deepEqual(blocks[3].inlines[0].at(-1), { type: 'bold', value: '粗' });
});
