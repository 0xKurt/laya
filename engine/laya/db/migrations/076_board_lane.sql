-- Kanban lane of a card on the Board view: `backlog`, `todo` or `in_progress`.
--
-- NULL means backlog: new cards land there. Done and archived are card statuses,
-- not lanes; a card running an agent shows under In progress whatever its lane.
ALTER TABLE action_cards ADD COLUMN board_lane TEXT;
