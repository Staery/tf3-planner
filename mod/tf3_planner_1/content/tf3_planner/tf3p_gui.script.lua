-- guiUpdate of tf3p_gui.gs.lua. No longer does anything: highlights are applied from the GUI via
-- react.enqueueJoin (see blueprint.lua B.defer). Kept because saves made with the previous version
-- contain an entity of this game script.
function data()
	return {
		guiUpdate = function(_userParams, _state, _guiState) end,
	}
end
