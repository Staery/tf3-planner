-- Game script of TF3 Planner: only a GUI-side update. The game UI highlight API
-- (api.gui.byId.setHighlighted) refuses calls from a react step timer ("API is currently restricted")
-- but accepts them here, the same place the game's own missions use (mission_sim.script@guiUpdate).
function data()
	return {
		guiUpdateScript = {
			fileName = "tf3p_gui.script@guiUpdate",
		},
	}
end
