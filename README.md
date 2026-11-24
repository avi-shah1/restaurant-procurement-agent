# Restaurant Procurement Agent

An AI procurement agent for restaurants. It forecasts what a kitchen will run out of, searches the market for better supplier prices, and recommends what to order. A person approves every decision.

**Live demo: https://restaurant-procurement-agent-xi.vercel.app/**

The demo uses built-in sample data (a fictional bistro, all prices in US dollars). Nothing is ordered and no real supplier is contacted.

See `ARCHITECTURE.md` for the design.

## How it works

1. **Forecast.** Python works out expected demand from usage history and flags items at risk of stocking out. All arithmetic is deterministic. The agent never does the maths.
2. **Run.** On an at-risk item, **Run** starts a procurement run. A ZooWork agent compares the options Python has priced, and Tavily searches the live web for market prices. Web prices are shown as unconfirmed, and anything a page did not state is shown as unknown.
3. **Review.** Each finished run shows a **Procurement Action Card**: current supplier vs recommended supplier, the saving in dollars and percent, reasons, risks, unknowns and sources.
4. **Approve.** **Approve RFQ** records the decision and creates a request-for-quote *draft*. It does not place an order. **Reject** records the rejection. **Send RFQ** then delivers the draft in mock/test mode, so nothing reaches a real supplier.

## Scenarios

`default`, `weekend_rush`, `coffee_spike`, `supplier_delay`, `avocado_shortage` change the demand and supply picture so you can see the agent respond.
