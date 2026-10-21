/** 폼 중복 클릭·본문 복사·원본 미리보기·키보드 지원 상품 보존 영역을 관리한다. */
document.querySelectorAll('form[method="post"]').forEach(form => {
  form.addEventListener('submit', event => {
    if (form.id === 'image-generation' && JSON.parse(document.getElementById('mask-points').value).length < 3) {
      event.preventDefault();
      document.getElementById('mask-message').textContent = '상품을 둘러싸는 점을 세 개 이상 표시해주세요';
      document.getElementById('mask-editor').focus();
      return;
    }
    if (form.dataset.submitting) { event.preventDefault(); return; }
    form.dataset.submitting = 'yes';
    const button = form.querySelector('button[type="submit"]');
    if (button) { button.disabled = true; button.textContent = '처리 중…'; }
  });
});
window.addEventListener('pageshow', event => {
  if (event.persisted) window.location.reload();
});
document.getElementById('copy-caption')?.addEventListener('click', async () => {
  const caption = document.getElementById('caption');
  const text = [caption.value, document.getElementById('hashtags').value].filter(Boolean).join('\n\n');
  try {
    await navigator.clipboard.writeText(text);
    document.getElementById('copy-message').textContent = '본문과 해시태그를 복사했습니다';
  } catch {
    caption.focus(); caption.select();
    document.getElementById('copy-message').textContent = '본문을 선택했습니다. 직접 복사해주세요.';
  }
});
let photoUrl;
document.getElementById('photo')?.addEventListener('change', event => {
  const file = event.target.files[0];
  const image = document.getElementById('local-photo');
  if (photoUrl) URL.revokeObjectURL(photoUrl);
  image.hidden = true;
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) {
    event.target.value = '';
    document.getElementById('file-message').textContent = '8MB 이하의 이미지를 업로드 해주세요';
    return;
  }
  document.getElementById('file-message').textContent = file.name;
  photoUrl = URL.createObjectURL(file); image.src = photoUrl; image.hidden = false;
});

const maskCanvas = document.getElementById('mask-editor');
const maskSource = document.getElementById('mask-source');
if (maskCanvas && maskSource) {
  const context = maskCanvas.getContext('2d');
  let points = [], cursor = [0.5, 0.5], keyboard = false;
  function drawMask() {
    if (!maskSource.complete || !maskSource.naturalWidth) return;
    context.drawImage(maskSource, 0, 0, maskCanvas.width, maskCanvas.height);
    if (points.length) {
      context.beginPath();
      points.forEach(([x, y], index) => context[index ? 'lineTo' : 'moveTo'](x * maskCanvas.width, y * maskCanvas.height));
      context.closePath(); context.fillStyle = '#3b7a5755'; context.fill();
      context.strokeStyle = '#245e3b'; context.lineWidth = 3; context.stroke();
      context.fillStyle = '#ffffff';
      points.forEach(([x, y]) => { context.beginPath(); context.arc(x * maskCanvas.width, y * maskCanvas.height, 5, 0, Math.PI * 2); context.fill(); });
    }
    if (keyboard) {
      const [x, y] = [cursor[0] * maskCanvas.width, cursor[1] * maskCanvas.height];
      context.strokeStyle = '#e46933'; context.lineWidth = 3; context.strokeRect(x - 6, y - 6, 12, 12);
    }
    document.getElementById('mask-points').value = JSON.stringify(points);
    document.getElementById('mask-message').textContent = `${points.length}개 점 · 선택한 안쪽을 보존합니다. 경계를 확인해주세요.`;
  }
  function addPoint(point) { if (points.length < 80) points.push(point); drawMask(); }
  function ready() {
    maskCanvas.width = Math.min(maskSource.naturalWidth, 960);
    maskCanvas.height = Math.round(maskCanvas.width * maskSource.naturalHeight / maskSource.naturalWidth);
    drawMask();
  }
  maskSource.addEventListener('load', ready);
  if (maskSource.complete && maskSource.naturalWidth) ready();
  maskCanvas.addEventListener('click', event => {
    const box = maskCanvas.getBoundingClientRect(); keyboard = false;
    addPoint([Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)), Math.max(0, Math.min(1, (event.clientY - box.top) / box.height))]);
  });
  maskCanvas.addEventListener('keydown', event => {
    const movement = { ArrowLeft: [-0.02, 0], ArrowRight: [0.02, 0], ArrowUp: [0, -0.02], ArrowDown: [0, 0.02] }[event.key];
    if (movement) { event.preventDefault(); keyboard = true; cursor = cursor.map((value, index) => Math.max(0, Math.min(1, value + movement[index]))); drawMask(); }
    if (event.key === 'Enter') { event.preventDefault(); addPoint([...cursor]); }
    if (event.key === 'Backspace') { event.preventDefault(); points.pop(); drawMask(); }
  });
  document.getElementById('mask-undo').addEventListener('click', () => { points.pop(); drawMask(); });
  document.getElementById('mask-reset').addEventListener('click', () => { points = []; drawMask(); });
}
if (document.querySelector('[data-image-pending]')) setTimeout(() => window.location.reload(), 5000);
