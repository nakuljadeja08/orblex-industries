// fakeSupabase.js — stands in for @supabase/supabase-js in the demo build (vite.config.js), so every
// view runs unchanged against sample rows. It answers the query methods the dashboard uses, and the
// buttons work on an in-memory copy that resets on reload. Nothing leaves the browser.
import { tables, DEMO_EMAIL, todayEt } from "./fixtures";

const db = tables();
const session = { access_token: "demo", user: { id: "demo", email: DEMO_EMAIL } };

function like(value, pattern) {
  const re = new RegExp("^" + pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/%/g, ".*") + "$");
  return re.test(String(value));
}

const OPS = {
  eq: (v, x) => String(v) === String(x),
  neq: (v, x) => String(v) !== String(x),
  gt: (v, x) => v > x, gte: (v, x) => v >= x, lt: (v, x) => v < x, lte: (v, x) => v <= x,
  like,
};

class Query {
  constructor(table) { this.rows = [...(db[table] || [])]; this.one = false; this.sorts = []; this.max = null; this.from_ = 0; }
  select() { return this; }
  eq(k, x)  { return this.where(r => OPS.eq(r[k], x)); }
  neq(k, x) { return this.where(r => OPS.neq(r[k], x)); }
  gt(k, x)  { return this.where(r => OPS.gt(r[k], x)); }
  gte(k, x) { return this.where(r => OPS.gte(r[k], x)); }
  lt(k, x)  { return this.where(r => OPS.lt(r[k], x)); }
  lte(k, x) { return this.where(r => OPS.lte(r[k], x)); }
  in(k, xs) { return this.where(r => xs.map(String).includes(String(r[k]))); }
  not(k, op, x) { return this.where(r => !OPS[op](r[k], x)); }
  // The one form the dashboard uses: "plan_date.eq.X,status.in.(A,B)".
  or(expr) {
    const parts = expr.match(/\w+\.(?:in\.\([^)]*\)|\w+\.[^,]*)/g) || [];
    return this.where(r => parts.some(p => {
      const [k, op, ...rest] = p.split(".");
      const val = rest.join(".");
      return op === "in" ? val.slice(1, -1).split(",").includes(String(r[k])) : OPS[op](r[k], val);
    }));
  }
  where(fn) { this.rows = this.rows.filter(fn); return this; }
  order(k, opts = {}) { this.sorts.push([k, opts.ascending !== false]); return this; }
  limit(n) { this.max = n; return this; }
  range(a, b) { this.from_ = a; this.max = b - a + 1; return this; }
  single() { this.one = true; return this; }
  maybeSingle() { this.one = true; return this; }
  result() {
    let out = this.rows;
    if (this.sorts.length) {
      out = [...out].sort((a, b) => {
        for (const [k, asc] of this.sorts) {
          if (a[k] === b[k]) continue;
          if (a[k] == null) return 1;
          if (b[k] == null) return -1;
          return (a[k] < b[k] ? -1 : 1) * (asc ? 1 : -1);
        }
        return 0;
      });
    }
    out = out.slice(this.from_, this.max == null ? undefined : this.from_ + this.max);
    return { data: this.one ? out[0] ?? null : out, error: null };
  }
  then(ok, bad) { return Promise.resolve(this.result()).then(ok, bad); }
}

const RPC = {
  approve_trade_order({ p_order_id, p_approve }) {
    const o = db.trade_orders.find(r => r.id === p_order_id);
    if (o) Object.assign(o, { status: p_approve ? "APPROVED" : "REJECTED", approved_by: DEMO_EMAIL, approved_at: new Date().toISOString() });
    return o;
  },
  set_trading_halt({ p_halted, p_reason }) {
    return Object.assign(db.trading_controls[0], { halted: p_halted, halt_reason: p_reason });
  },
  set_trading_mandate({ p_max_entries, p_max_total_risk_usd, p_tickers }) {
    const m = { mandate_date: todayEt(), max_entries: p_max_entries, max_total_risk_usd: p_max_total_risk_usd, allowed_tickers: p_tickers,
                order_types: ["LIMIT"], require_stop: true, expires_at: new Date(Date.now() + 864e5).toISOString(), status: "ACTIVE",   // so it works after the close too
 approved_by: DEMO_EMAIL,
                approved_at: new Date().toISOString(), revoked_at: null };
    db.trading_mandates = [m];
    return m;
  },
  revoke_trading_mandate() {
    return Object.assign(db.trading_mandates[0] || {}, { status: "REVOKED", revoked_at: new Date().toISOString() });
  },
};

export function createClient() {
  return {
    from: t => new Query(t),
    rpc: async (name, args) => ({ data: RPC[name]?.(args) ?? null, error: null }),
    auth: {
      getSession: async () => ({ data: { session } }),
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
      signInWithOtp: async () => ({ error: null }),
      signOut: async () => ({ error: null }),
    },
  };
}
