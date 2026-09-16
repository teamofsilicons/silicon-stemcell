// Space Station web SDK vendored at b308faf88784b5ea6d657878b7419383b3eb7601.
import {createSpaceStationWeb} from './space-station-web.js';
const toggle = document.getElementById('docs-telemetry');
let enabled = true;
try { enabled = localStorage.getItem('silicon-docs-telemetry') !== 'off'; } catch {}
const telemetry = createSpaceStationWeb({analyticsTable:'silicondocs', eventsTable:'silicondocs', endpoint:'/api/telemetry', enabled});
if(toggle) {
  toggle.checked = enabled;
  toggle.addEventListener('change', () => {
    telemetry.setEnabled(toggle.checked);
    try { localStorage.setItem('silicon-docs-telemetry', toggle.checked ? 'on' : 'off'); } catch {}
  });
}
document.addEventListener('click', event => {
  const anchor = event.target.closest('a');
  if (anchor?.getAttribute('href')?.includes('/releases/download/')) telemetry.track('installer_link');
});
