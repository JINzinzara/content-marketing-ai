/** 서버와 같은 규칙으로 입력 중인 본문과 태그의 중복 노출을 막는다. */
function captionParts(caption, hashtags) {
  const pattern = /(?<![\p{L}\p{N}_#])#[\p{L}\p{N}_]+/gu;
  const seen = new Set();
  function unique(tag) {
    const key = tag.toLowerCase();
    if (seen.has(key)) return '';
    seen.add(key); return tag;
  }
  const tags = hashtags.replace(pattern, unique).trim();
  const extra = (caption.match(pattern) || []).map(unique).filter(Boolean);
  const body = caption.replace(pattern, '').split('\n').map(line => line.trimEnd()).join('\n').trim().replace(/\n[ \t]*\n(?:[ \t]*\n)+/g, '\n\n');
  return [body, `${tags} ${extra.join(' ')}`.trim().replace(/\s+/g, ' ')];
}
