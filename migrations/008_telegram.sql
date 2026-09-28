-- The Telegram message a run reports to: the chat, and the one message that
-- is edited in place as the run's events land. NULL for a run nobody asked
-- for from Telegram.
ALTER TABLE runs ADD COLUMN telegram_chat_id bigint;
ALTER TABLE runs ADD COLUMN telegram_message_id bigint;

-- Conversation memory: the last 20 turns per chat, per docs/mercury.md.
-- Older turns are deleted as new ones arrive.
CREATE TABLE telegram_turns (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_id    bigint NOT NULL,
    role       text NOT NULL CHECK (role IN ('user', 'assistant')),
    text       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX telegram_turns_chat ON telegram_turns (chat_id, id DESC);
