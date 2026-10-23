# 깍두기 도면체 (Kkakdugi Drafting)

도면에서도 한글의 아름다움을 표현하기 위해 한땀 한땀 그려서 만든 깍두기 도면체입니다.

- 소개 페이지 : https://dynamic-potato.github.io/kkakdugi-drafting/

![깍두기 도면체](images/hero.svg)


## 글자 범위

| 묶음 | 수 |
|---|---|
| 한글 완성형 (KS X 1001 및 현대 한글 전량. 11,172자) + 호환 자모 51자 | 11,223 |
| 라틴 기본/확장 일부, 그리스, 키릴 일부 | 약 500 |
| 일본어 가나 (히라가나 84 + 가타카나 86, 탁음/반탁음/소형 포함) | 170 |
| 도면/공학 기호 (Ø ± ° ℃ ⌀ ∠ ⊥ ∥ ▽ ◇ 원문자 ①~⑳, 물결표 등) | 약 300 |
| 수학 기호 (연산/관계/적분/화살표/괄호 등 공학 수학 범위) + 수식 조판(OpenType MATH) | 약 220 |
| 수학 이탤릭/굵은 문자 (수식 편집기가 변수/벡터에 자동으로 사용) | 230 |
| 전기 기호 (⏚ ⎓ ∿ ⏻ ⚠ 등) + 회로 기호 (저항, 커패시터, 다이오드, 트랜지스터, 논리 게이트 등) | 44 |
| 괘선 (─ ━ │ ┃ ┌ ┏ ╔ ═ ╭ ╱ 등) | 134 |
| 블록 요소 (▀ ▄ █ ▌ ▐ ░ ▒ ▓ 등) | 32 |


## 굵기

- ExtraLight (250)
- Light (300)
- Regular (400)
- Bold (700)


## 설치

`fonts/ttf/` 내 TTF 4개를 설치하면 됩니다.


## CAD에서 사용하기
※ 세필 느낌을 원하시면 ExtraLight, 기존 CAD 서체와 비슷한 느낌을 원하시면 Light를 추천드립니다.

### 설치
- Windows : TTF 4개 선택 → 마우스 오른쪽 → 모든 사용자용으로 설치
- macOS : TTF 더블 클릭 → 서체 관리자에서 설치
- 설치 후 CAD 프로그램을 다시 실행하면 글꼴 목록에 [깍두기 도면체, 깍두기 도면체 Light, 깍두기 도면체 ExtraLight]로 나타납니다.

### FreeCAD (TechDraw)
- 환경 설정 > TechDraw > 일반 > 라벨 글꼴 → "깍두기 도면체 Light 또는 ExtraLight"<br>
  ※ 라벨과 새로 만드는 치수에 적용됩니다. 이미 만든 치수에는 적용되지 않습니다.

### KiCad (PCB 실크)
- 텍스트 속성의 Font 목록 → "깍두기 도면체"
- 다른 PC에서 프로젝트를 열 때도 같은 글꼴로 보이게 하려면 Board Setup에서 Embed fonts를 켭니다.

※ PCB 실크 스크린의 경우 Light, ExtraLight로 하면 획이 얇아서 인쇄가 안될 가능성이 있습니다.

### 자간
- 깍두기 도면체는 글자 폭 안에 자체 간격이 들어 있어 자간과 폭 비율을 기본 값으로 두셔야 전각/반각 정렬됩니다.


## 깍두기 도면체의 특징

- 구분성 : [1, I, i, |], [0, O, o, ㅇ] 등 비슷한 글자들로 인해 **혼동되지 않도록 글자를 설계**하여 확실한 구분감을 제공합니다.
- 정렬성 : 모든 글자가 **반각/전각/초전각**으로 되어 있어 어떤 글을 쓰든 정렬감을 제공합니다.
- 명확성 : 도면을 돌려 볼 때 6과 9가 헷갈리지 않도록 **두 숫자 아래에 짧은 밑줄**을 두어 어느 방향에서 보아도 헷갈리지 않습니다.


## 라이선스

- SIL Open Font License 1.1 : `OFL.txt`
- 개인/상업 용도 모두 무료 및 수정/재배포 자유
- 글꼴 단독 판매 금지
- 예약 글꼴 이름(RFN) : 「Kkakdugi Drafting」, 「깍두기 도면체」 (1.002부터)
- 추가 허락 : 웹페이지/앱/게임 안에 넣어 쓰려고 필요한 글자만 남기거나(서브셋) 웹폰트 형식으로 바꾼 경우, 글자 모양을 고치지 않았다면 위 이름을 그대로 써도 됩니다. 단, 이렇게 만든 파일을 설치용 글꼴 파일로 따로 배포할 때는 이름을 바꿔야 합니다.


## 만든 사람

- Dynamic Potato (역동감자)
- dynamicpotato7@gmail.com
- https://github.com/dynamic-potato/kkakdugi-drafting


---
[Machine-translated]

# Kkakdugi Drafting

Kkakdugi Drafting is a typeface drawn one stroke at a time to bring the beauty of Hangul to technical drawings.


## Character coverage

| Group | Count |
|---|---|
| Hangul syllables (KS X 1001 and all modern Hangul, 11,172) + 51 compatibility jamo | 11,223 |
| Basic Latin/part of Latin Extended, Greek, part of Cyrillic | about 500 |
| Japanese kana (84 hiragana + 86 katakana, including voiced and small forms) | 170 |
| Drawing/engineering symbols (Ø ± ° ℃ ⌀ ∠ ⊥ ∥ ▽ ◇, circled numbers ①–⑳, wave dash, etc.) | about 300 |
| Math symbols (operators/relations/integrals/arrows/brackets for engineering math) + math typesetting (OpenType MATH) | about 220 |
| Math italic/bold letters (used automatically by equation editors for variables/vectors) | 230 |
| Electrical symbols (⏚ ⎓ ∿ ⏻ ⚠, etc.) + circuit symbols (resistor, capacitor, diode, transistor, logic gates, etc.) | 44 |
| Box drawing (─ ━ │ ┃ ┌ ┏ ╔ ═ ╭ ╱, etc.) | 134 |
| Block elements (▀ ▄ █ ▌ ▐ ░ ▒ ▓, etc.) | 32 |


## Weights

- ExtraLight (250)
- Light (300)
- Regular (400)
- Bold (700)


## Installation

Install the four TTF files in `fonts/ttf/`.


## Using it in CAD
※ For a fine-pen look we recommend ExtraLight; for a look closer to existing CAD fonts, Light.

### Installation
- Windows : select the 4 TTF files → right-click → Install for all users
- macOS : double-click the TTF → install in Font Book
- After installing, restart your CAD program. The font appears in the font list as [Kkakdugi Drafting, Kkakdugi Drafting Light, Kkakdugi Drafting ExtraLight].

### FreeCAD (TechDraw)
- Preferences > TechDraw > General > Label Font → "Kkakdugi Drafting Light or ExtraLight"<br>
  ※ Applies to labels and newly created dimensions. Existing dimensions are not changed.

### KiCad (PCB silkscreen)
- Text properties → Font list → "Kkakdugi Drafting"
- To show the same font when the project is opened on another PC, turn on Embed fonts in Board Setup.

※ For PCB silkscreen, Light and ExtraLight strokes may be too thin to print.

### Letter spacing
- Kkakdugi Drafting has its own spacing built into each character width. Keep tracking and width factor at their defaults to stay aligned on the half-width/full-width grid.


## Features

- Distinction : Glyphs are **designed so that look-alike characters** such as [1, I, i, |] and [0, O, o, ㅇ] **are not confused**, so each one stays clearly distinct.
- Alignment : Every character is **half-width, full-width or triple half-width**, so whatever you write lines up.
- Clarity : **Both 6 and 9 have a short underline**, so they are not confused when a drawing is rotated, whichever way you look at it.


## License

- SIL Open Font License 1.1 : `OFL.txt`
- Free for personal and commercial use; free to modify and redistribute
- The font files may not be sold by themselves
- Reserved Font Names: "Kkakdugi Drafting", "깍두기 도면체" (from 1.002)
- Additional permission: If you subset the font or convert it to a webfont format to use it inside a web page, app or game, you may keep these names as long as the glyph shapes are unchanged. If you distribute such a file separately as an installable font, you must rename it.


## Author

- Dynamic Potato (역동감자)
- dynamicpotato7@gmail.com
- https://github.com/dynamic-potato/kkakdugi-drafting
