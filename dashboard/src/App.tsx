import { Navigate, Route, Routes } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import DriverLayout from '@/layouts/DriverLayout'
import ManagerLayout from '@/layouts/ManagerLayout'
import Billing from '@/pages/manager/Billing'
import Data from '@/pages/manager/Data'
import DispatchBoard from '@/pages/manager/DispatchBoard'
import Drivers from '@/pages/manager/Drivers'
import FleetHealth from '@/pages/manager/FleetHealth'
import SimulationShowcase from '@/pages/manager/SimulationShowcase'
import SimulationTrip from '@/pages/manager/SimulationTrip'
import TripHistory from '@/pages/manager/TripHistory'

import DriverHome from '@/pages/driver/Home'
import Inspection from '@/pages/driver/Inspection'
import MyStats from '@/pages/driver/MyStats'
import MyTruck from '@/pages/driver/MyTruck'
import SignIn from '@/pages/SignIn'
import { RequireRole } from '@/routes/RequireRole'

export default function App() {
  return (
    <TooltipProvider delayDuration={150}>
      <Routes>
        <Route path="/" element={<SignIn />} />

        <Route
          path="/manager"
          element={
            <RequireRole role="manager">
              <ManagerLayout />
            </RequireRole>
          }
        >
          {/* Real user ask: Live Ops (the always-on live.* telemetry feed) is dropped -- the
              simulation is now the app's only data source. Simulation Showcase (the AI-dispatch
              full-day replay) takes over the Live Ops slot, renamed "Live Ops Simulation". */}
          <Route index element={<SimulationShowcase />} />
          <Route path="dispatch" element={<DispatchBoard />} />
          <Route path="trips" element={<TripHistory />} />
          <Route path="fleet-health" element={<FleetHealth />} />
          <Route path="drivers" element={<Drivers />} />
          <Route path="billing" element={<Billing />} />
          <Route path="data" element={<Data />} />
          <Route path="simulation-trip" element={<SimulationTrip />} />
        </Route>

        <Route
          path="/driver"
          element={
            <RequireRole role="driver">
              <DriverLayout />
            </RequireRole>
          }
        >
          <Route index element={<DriverHome />} />
          <Route path="inspection" element={<Inspection />} />
          <Route path="stats" element={<MyStats />} />
          <Route path="truck" element={<MyTruck />} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </TooltipProvider>
  )
}
