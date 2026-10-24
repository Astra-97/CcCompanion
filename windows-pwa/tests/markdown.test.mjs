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

test('regression 2026-10-24: xiaoke subtitle reply with a bold-wrapped Windows path parses into plain flow blocks', () => {
  // Astra's clipped bubble: the overflow came from a folded tool line widening
  // the message track (pinned in shell.test.mjs), not from this markdown.  Pin
  // that the reply only yields paragraphs/lists of text, bold and code inlines
  // — no fenced code block whose pre could carry an unbreakable min-content.
  const source = '下 **mp4 可以**，有纯音频（mp3/m4a/wav）更好，文件小，我也是只取声音。\n\n下好以后放进 **`E:\\gpt\\乙抓\\紫月园`**（新建个文件夹就行），或者直接在 app 里发给我也行。\n\n我这边接着做：\n1. 用 ElevenLabs 的 Scribe 把日语听写出来，带时间轴\n2. 翻成中文，按哥哥那种口吻润色，不搞机翻腔\n3. 做成 **中日双语 srt 字幕**\n\n你想要哪种：\n- **外挂字幕**：一个 srt 文件，播放器一起加载，以后想改字也方便\n- **压进视频**：在你电脑上用 ffmpeg 把字幕烧进 mp4，手机上直接看\n\n你先下着，下好了跟我说一声 🎧💜';
  const blocks = parseMarkdown(source);
  assert.deepEqual(blocks.map(({ type, ordered }) => type + (type === 'list' ? (ordered ? ':ol' : ':ul') : '')), ['paragraph', 'paragraph', 'paragraph', 'list:ol', 'paragraph', 'list:ul', 'paragraph']);
  assert.ok(!blocks.some(({ type }) => type === 'code'), 'no fenced code block in this reply');
  const pathLine = blocks[1].inlines[0];
  assert.ok(pathLine.some((token) => /E:\\gpt\\乙抓\\紫月园/.test(token.value)), 'the backslash path survives verbatim');
  assert.deepEqual(parseInline('放进 `E:\\gpt\\乙抓\\紫月园` 里'), [
    { type: 'text', value: '放进 ' },
    { type: 'code', value: 'E:\\gpt\\乙抓\\紫月园' },
    { type: 'text', value: ' 里' },
  ]);
});
