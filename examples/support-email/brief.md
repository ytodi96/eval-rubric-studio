# Brief: support email replies for Brightside Coffee Co.

> Brightside Coffee Co. is a fictional company, invented for this demo.

Brightside is a small online coffee roaster in the US. It sells whole-bean and ground coffee in 250 g
and 1 kg bags, one-off or as a subscription delivered every 2 or 4 weeks.

The support inbox gets 40–80 emails a day: "where is my order", damaged bags, refund requests,
subscription changes (skip, pause, cancel), questions about the coffee itself, and the occasional
angry customer. A support agent currently writes every reply by hand.

We want an AI assistant that drafts the reply to each email. A person reads the draft and sends it.
The assistant gets the customer's email and, when we have it, the order details (order number,
items, total, order date, delivery status from tracking).

A good reply is warm, short, correct about our policies, and tells the customer exactly what
happens next. It must never promise something the policy doesn't allow (discounts, exact delivery
dates, refunds on opened bags), never invent order details, and never share anything about other
customers. Anything that needs a human (legal threats, chargebacks, big refunds) should be
handed over to the team lead instead of handled.

The policy the assistant uses today is in `system_prompt_v1.md`. It is a reasonable first draft,
not a perfect one; the Studio's job is to find where it falls short.
