-- Sound files: the doorbells and the stream (Core.lua, Stream.lua).

C_CVar = { GetCVarBool = function(name) return name == "Sound_EnableAllSound" and client.sound end }

local nextHandle = 0

function PlaySoundFile(path)
	-- Like the real client, a file played twice in one frame is only read from disk once.
	if client.openedThisFrame[path] then
		nextHandle = nextHandle + 1
		client.playing[nextHandle] = client.validSounds[path] == true
		return true, nextHandle
	end
	client.openedThisFrame[path] = true
	local data = client.ReadFile(client.DiskPath(path))
	if not data then return false end
	if data:sub(1, 4) == "RIFF" then client.validSounds[path] = true end
	nextHandle = nextHandle + 1
	client.playing[nextHandle] = client.validSounds[path] == true
	return true, nextHandle
end
function StopSound(handle) client.playing[handle] = false end
C_Sound = { IsPlaying = function(handle) return client.playing[handle] == true end }

