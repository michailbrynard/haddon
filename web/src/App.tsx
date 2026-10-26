import { Navigate, Route, Routes } from "react-router-dom"

import { AppShell } from "@/components/AppShell"
import { Account } from "@/routes/Account"
import { Accounts } from "@/routes/Accounts"
import { Close } from "@/routes/Close"
import { Entities } from "@/routes/Entities"
import { Entry } from "@/routes/Entry"
import { LineDetail } from "@/routes/LineDetail"
import { Month } from "@/routes/Month"
import { NewEntry } from "@/routes/NewEntry"
import { Reports } from "@/routes/Reports"
import { Review } from "@/routes/Review"
import { Statements } from "@/routes/Statements"
import { TrialBalance } from "@/routes/TrialBalance"
import { EmptyState } from "@/components/states"

/** Client-side routes. The service returns `index.html` for any path it doesn't serve itself, so these survive
 *  a reload and can be linked to directly (a review link can name a month). */
export function App() {
  return (
    <Routes>
      <Route path="/" element={<Entities />} />
      <Route
        path="/e/:entity"
        element={
          <AppShell>
            <Routes>
              <Route index element={<Navigate to="month" replace />} />
            </Routes>
          </AppShell>
        }
      />
      <Route
        path="/e/:entity/*"
        element={
          <AppShell>
            <Routes>
              <Route path="month" element={<Month />} />
              <Route path="review" element={<Review />} />
              <Route path="accounts" element={<Accounts />} />
              <Route path="close" element={<Close />} />
              <Route path="statements" element={<Statements />} />
              <Route path="accounts/:accountId" element={<Account />} />
              <Route path="reports" element={<Reports />} />
              <Route path="trial-balance" element={<TrialBalance />} />
              <Route path="lines/:lineId" element={<LineDetail />} />
              <Route path="entries/new" element={<NewEntry />} />
              <Route path="entries/:entryId" element={<Entry />} />
              <Route path="*" element={<Navigate to="month" replace />} />
            </Routes>
          </AppShell>
        }
      />
      <Route
        path="*"
        element={
          <div className="mx-auto max-w-2xl px-5 py-16">
            <EmptyState title="There is nothing at this address" />
          </div>
        }
      />
    </Routes>
  )
}
