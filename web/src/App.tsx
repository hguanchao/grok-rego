import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/layout";

const RegisterPage = lazy(() =>
  import("@/pages/RegisterPage").then((module) => ({ default: module.RegisterPage })),
);
const PoolPage = lazy(() =>
  import("@/pages/PoolPage").then((module) => ({ default: module.PoolPage })),
);
const GatewayPage = lazy(() =>
  import("@/pages/GatewayPage").then((module) => ({ default: module.GatewayPage })),
);
const UsagePage = lazy(() =>
  import("@/pages/UsagePage").then((module) => ({ default: module.UsagePage })),
);

export default function App() {
  return (
    <Suspense fallback={null}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/register" replace />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/pool" element={<PoolPage />} />
          <Route path="/gateway" element={<GatewayPage />} />
          <Route path="/usage" element={<UsagePage />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
