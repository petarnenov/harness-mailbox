import { realpath } from 'node:fs/promises';
import { joinSession } from '@github/copilot-sdk/extension';
import { createOfferPublisher, createRelay } from './transport.mjs';

try {
  const session = await joinSession({});
  const { HARNESS_MAILBOX_ROOT: root, HARNESS_MAILBOX_ROUTE: route,
    HARNESS_MAILBOX_CWD: cwd } = process.env;
  if (await realpath(process.cwd()) !== await realpath(cwd)) throw new Error();
  const participant = { root, route, cwd, sessionId: session.sessionId };
  const relay = await createRelay({ ...participant, send: notification => session.send(notification) });
  const offer = route === 'orchestrator' ? undefined : createOfferPublisher(participant);
  await relay.run({ beforePoll: offer, warn: message => console.error(message) });
} catch {
  console.error('Copilot mailbox notifications could not start. Check the launcher and project registration.');
  process.exitCode = 1;
}
