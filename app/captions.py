"""생성·수정·미리보기·게시가 같은 본문과 고유 태그를 사용한다."""

import re

TAG = re.compile(r"(?<![\w#])#[\w]+")


def caption_parts(caption, hashtags):
    seen = set()

    def unique(tag):
        key = tag.lower()
        if key in seen:
            return ""
        seen.add(key)
        return tag

    tags = TAG.sub(lambda match: unique(match.group()), hashtags).strip()
    extra = [value for tag in TAG.findall(caption) if (value := unique(tag))]
    body = TAG.sub("", caption)
    body = "\n".join(line.rstrip() for line in body.splitlines()).strip()
    body = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", body)
    return body, " ".join((tags + " " + " ".join(extra)).split())


def caption_text(post):
    return "\n\n".join(part for part in caption_parts(post["caption"], post["hashtags"]) if part)
