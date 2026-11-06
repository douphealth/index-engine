-- Why the engine proposes a link: a page not indexed yet, or a page close to the top for a search (its wording is the anchor).
ALTER TABLE link_proposals ADD COLUMN why TEXT;
