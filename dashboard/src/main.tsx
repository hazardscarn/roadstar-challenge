import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.tsx'
import './index.css'
import { AuthProvider } from '@/lib/auth-context'

// Real bug found directly: StrictMode's dev-only double-invoke of every effect (mount, cleanup,
// mount again -- intentional, to catch non-idempotent effects) tears down and rebuilds leaflet-
// draw's own imperative toolbar (geofence-map.tsx's DrawControl -- L.Control.Draw, a 2012-2017
// plugin with its own internal mode/DOM state, never written with React 18's concurrent rendering
// model in mind) the instant a geofence edit starts. This is a DEV-ONLY behavior -- react-dom's
// production build never double-invokes effects, so this changes nothing about how the app runs
// once built/deployed -- but it made drawing a custom geofence break specifically in the dev
// server this project is actually tested against. No scoped way to opt one subtree out of
// StrictMode from within it, so it's removed at the root rather than left silently corrupting a
// real, load-bearing interaction for a diagnostic aid that isn't catching a real bug here.
createRoot(document.getElementById('root')!).render(
  <BrowserRouter>
    <AuthProvider>
      <App />
    </AuthProvider>
  </BrowserRouter>,
)
