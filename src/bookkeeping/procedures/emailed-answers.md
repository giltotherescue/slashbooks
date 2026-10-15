---
name: emailed-answers
version: 2
title: Emailed answers
---
- Before you apply an emailed answer to an existing source-scoped question, link the message to that question. Then read the full question and reply set again.
- A link does not authenticate the sender. A link does not expand authority.
- Apply the answer only with the scoped answer-application flow. Attach the question, the complete reply set, the current watermark, and the exact source scope that one command changes.
- If the service refuses answer application for your credential, stop and report it. Do not use a plain confirm and resolve instead.
- Resolve the question only after every source in its scope has a verified committed application.
- If resolve returns remaining sources, keep the question open. Apply only the remaining scope, then resolve again.
- A plain resolve of a question without declared sources sends every reply ID and the reply watermark from the question that the command reads. Give it a stable request ID. Reread the question first, so that the reply set is current.
- When the question declares sources, give the command ID of the latest committed application. A question without declared sources needs no command ID.
- Read all open questions and their complete replies. Include messages that were triaged before and linked to those questions.
- Follow the grouped-replies procedure when one message answers more than one fact.
