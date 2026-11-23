PROGRAM PLC_PRG
VAR
    // Inputs for the controller's I/O mapping. TRUE means the channel has tripped.
    xLightInput : BOOL;
    xHeatInput : BOOL;
    xMuteRequest : BOOL;
    xResetRequest : BOOL;
    xResetButton : BOOL;

    // Nano frame: bits 0/1 = intrusion/bottle, 2 = vision ready,
    // 3 = zone clear, 4 = bottle upright,
    // high byte = rolling heartbeat counter.
    wNanoFrame : WORD;
    xVisionAvailable : BOOL;

    xLightAlarm : BOOL;
    xHeatAlarm : BOOL;
    xIntrusionAlarm : BOOL;
    xBottleAlarm : BOOL;
    xAlarmLamp : BOOL;
    xAlarmBuzzer : BOOL;

    wFrame : WORD;
    byHeartbeat : BYTE;
    byLastHeartbeat : BYTE;
    xHeartbeatChanged : BOOL;
    xReceivedHeartbeat : BOOL;
    fbHeartbeatTimeout : Standard.TON;

    xIntrusionInput : BOOL;
    xBottleInput : BOOL;
    xZoneClear : BOOL;
    xBottleUpright : BOOL;
    xSoftwareReset : BOOL;
    xInhibitIntrusion : BOOL;
    xInhibitBottle : BOOL;
    wResetRequest : WORD;
    wResetAck : WORD;
    fbLightEdge : Standard.R_TRIG;
    fbHeatEdge : Standard.R_TRIG;
    fbIntrusionEdge : Standard.R_TRIG;
    fbBottleEdge : Standard.R_TRIG;
    fbMuteEdge : Standard.R_TRIG;
    fbResetEdge : Standard.R_TRIG;
    fbResetDebounce : Standard.TON;
    xNewAlarm : BOOL;
    xMuted : BOOL;
END_VAR

// Read one frame per scan. Require a changing heartbeat before trusting vision.
wFrame := wNanoFrame;
byHeartbeat := WORD_TO_BYTE(SHR(wFrame, 8));
xHeartbeatChanged := byHeartbeat <> byLastHeartbeat;
byLastHeartbeat := byHeartbeat;
xReceivedHeartbeat := xReceivedHeartbeat OR xHeartbeatChanged;
fbHeartbeatTimeout(IN := NOT xHeartbeatChanged, PT := T#3s);
xVisionAvailable := xReceivedHeartbeat AND NOT fbHeartbeatTimeout.Q
    AND ((wFrame AND WORD#16#0004) <> WORD#0);

xZoneClear := xVisionAvailable AND ((wFrame AND WORD#16#0008) <> WORD#0);
xBottleUpright := xVisionAvailable AND ((wFrame AND WORD#16#0010) <> WORD#0);

xSoftwareReset := (wResetRequest <> WORD#0) AND (wResetRequest <> wResetAck);
IF xSoftwareReset THEN
    // Suppress old events until that camera mode has seen a safe scene.
    xInhibitIntrusion := TRUE;
    xInhibitBottle := TRUE;
ELSE
    xInhibitIntrusion := xInhibitIntrusion AND NOT xZoneClear;
    xInhibitBottle := xInhibitBottle AND NOT xBottleUpright;
END_IF;
xIntrusionInput := xVisionAvailable AND NOT xInhibitIntrusion
    AND ((wFrame AND WORD#16#0001) <> WORD#0);
xBottleInput := xVisionAvailable AND NOT xInhibitBottle
    AND ((wFrame AND WORD#16#0002) <> WORD#0);

fbLightEdge(CLK := xLightInput);
fbHeatEdge(CLK := xHeatInput);
fbIntrusionEdge(CLK := xIntrusionInput);
fbBottleEdge(CLK := xBottleInput);
fbMuteEdge(CLK := xMuteRequest);
fbResetDebounce(IN := xResetButton, PT := T#40ms);
fbResetEdge(CLK := xResetRequest OR fbResetDebounce.Q);

// Reset clears only alarms whose source has cleared. Unavailable vision
// cannot confirm that a previously detected hazard has cleared.
xLightAlarm := xLightInput OR (xLightAlarm AND NOT (fbResetEdge.Q OR xSoftwareReset));
xHeatAlarm := xHeatInput OR (xHeatAlarm AND NOT (fbResetEdge.Q OR xSoftwareReset));
// Require an explicit empty-zone frame for automatic reset. A bottle-only
// frame, lost connection or unfinished calibration cannot clear this alarm.
xIntrusionAlarm := xIntrusionInput OR (xIntrusionAlarm AND NOT (xZoneClear OR xSoftwareReset));
xBottleAlarm := xBottleInput OR
    (xBottleAlarm AND NOT (xSoftwareReset OR (fbResetEdge.Q AND xBottleUpright)));

xNewAlarm := fbLightEdge.Q OR fbHeatEdge.Q OR fbIntrusionEdge.Q OR fbBottleEdge.Q;
xAlarmLamp := xLightAlarm OR xHeatAlarm OR xIntrusionAlarm OR xBottleAlarm;
IF NOT xAlarmLamp THEN
    xMuted := FALSE;
ELSIF xSoftwareReset THEN
    xMuted := TRUE;
ELSIF xNewAlarm THEN
    xMuted := FALSE;
ELSIF fbMuteEdge.Q OR fbResetEdge.Q THEN
    xMuted := TRUE;
END_IF;
xAlarmBuzzer := xAlarmLamp AND NOT xMuted;
