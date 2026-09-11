import * as L from 'leaflet'

// Real user feedback: dots don't read as trucks, and status needs to be legible at a glance --
// a real fleet manager scanning a map full of markers needs the SHAPE to say "truck" and the
// COLOR to say status, matching real fleet-telematics map conventions (Samsara/Motive-style).
const TRUCK_SVG = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="white" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M1 4h13v11H1z" fill="white" fill-opacity="0.15"/>
  <path d="M14 9h4l3 3v3h-7z" fill="white" fill-opacity="0.15"/>
  <circle cx="5.5" cy="17.5" r="1.6" fill="white"/>
  <circle cx="17.5" cy="17.5" r="1.6" fill="white"/>
</svg>`

export function truckIcon(color: string, selected = false) {
  const size = selected ? 34 : 28
  return L.divIcon({
    className: '',
    html: `<div style="
      width:${size}px;height:${size}px;border-radius:9999px;background:${color};
      display:flex;align-items:center;justify-content:center;
      border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,0.35);
    ">${TRUCK_SVG}</div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  })
}
