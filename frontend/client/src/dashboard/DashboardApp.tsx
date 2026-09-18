import { Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { Empty } from "./components/States";
import { Actions } from "./pages/Actions";
import { Alerts } from "./pages/Alerts";
import { AtRisk } from "./pages/AtRisk";
import { CommandCenter } from "./pages/CommandCenter";
import { OrderDetail } from "./pages/OrderDetail";
import { Orders } from "./pages/Orders";
import { PlanDetail } from "./pages/PlanDetail";
import { Recovery } from "./pages/Recovery";
import { WhatIf } from "./pages/WhatIf";
import "./dashboard.css";

/**
 * The operator dashboard, mounted at /dashboard/* behind ProtectedDashboard.
 *
 * Loaded lazily from App.tsx, so its code and stylesheet only download once
 * someone actually opens it. Every page is deep-linkable, so a refresh
 * mid-demo lands in the same place.
 */
export default function DashboardApp() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<CommandCenter />} />
        <Route path="orders" element={<Orders />} />
        <Route path="orders/:orderId" element={<OrderDetail />} />
        <Route path="at-risk" element={<AtRisk />} />
        <Route path="what-if" element={<WhatIf />} />
        <Route path="recovery" element={<Recovery />} />
        <Route path="recovery/:planId" element={<PlanDetail />} />
        <Route path="actions" element={<Actions />} />
        <Route path="alerts" element={<Alerts />} />
        <Route path="*" element={<Empty title="That page does not exist" />} />
      </Route>
    </Routes>
  );
}
