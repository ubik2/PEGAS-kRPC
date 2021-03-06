import numpy as np
import init_simulation
from launch_targeting import LaunchSite
from results_to_init import InitStruct
from unit import unit
from get_angle_from_frame import get_angle_from_frame
from approx_from_curve import approx_from_curve
from get_max_value import get_max_value
from get_orbital_elements import get_orbital_elements
from get_thrust import get_thrust
from rodrigues import rodrigues
from calculate_air_density import calculate_air_density
import unified_powered_flight_guidance as upfg

mu = init_simulation.mu
g0 = init_simulation.g0
R = init_simulation.R
atmpressure = init_simulation.atmpressure
atmtemperature = init_simulation.atmtemperature
period = init_simulation.period
convergence_criterion = init_simulation.convergence_criterion


# Helper function (built into MATLAB) -- note that elevation is not theta
def cart2sph(x, y, z):
    xxyy = x**2 + y**2
    r = np.sqrt(xxyy + z**2)
    elevation = np.arctan2(z, np.sqrt(xxyy))
    azimuth = np.arctan2(y, x)
    return np.array([azimuth, elevation, r])


# Helper function (built into MATLAB)
def sph2cart(azimuth, elevation, r):
    x = r * np.cos(elevation) * np.cos(azimuth)
    y = r * np.cos(elevation) * np.sin(azimuth)
    z = r * np.sin(elevation)
    return np.array([x, y, z])


class Orbit:
    def __init__(self, sma, ecc, inc, lan, aop, tan):
        self.sma = sma
        self.ecc = ecc
        self.inc = inc
        self.lan = lan
        self.aop = aop
        self.tan = tan


class PlotEntry:
    def __init__(self, t, r, rmag, v, vy, vt, vmag, F, a, q, pitch, yaw, vair, vairmag, angle_ps, angle_ys, angle_po, angle_yo):
        self.t = t
        self.r = r
        self.rmag = rmag
        self.v = v
        self.vy = vy
        self.vt = vt
        self.vmag = vmag
        self.F = F
        self.a = a
        self.q = q
        self.pitch = pitch
        self.yaw = yaw
        self.vair = vair
        self.vairmag = vairmag
        self.angle_ps = angle_ps
        self.angle_ys = angle_ys
        self.angle_po = angle_po
        self.angle_yo = angle_yo


class Result:
    def __init__(self, altitude, apoapsis, periapsis, orbit, velocity, velocity_y, velocity_t, 
                 max_Qv, max_Qt, lost_gravity, lost_drag, lost_total, burn_time_left, plots, eng):
        self.altitude = altitude
        self.apoapsis = apoapsis
        self.periapsis = periapsis
        self.orbit = orbit
        self.velocity = velocity
        self.velocity_y = velocity_y
        self.velocity_t = velocity_t
        self.max_Qv = max_Qv
        self.max_Qt = max_Qt
        self.lost_gravity = lost_gravity
        self.lost_drag = lost_drag
        self.lost_total = lost_total
        self.burn_time_left = burn_time_left
        self.plots = plots
        self.eng = eng
        self.upfg = None


class Control:
    def __init__(self, type):
        self.type = type


class GravityTurnControl(Control):
    def __init__(self, pitch, velocity, azimuth):
        Control.__init__(self, 0)
        self.pitch = pitch
        self.velocity = velocity
        self.azimuth = azimuth


class PitchControl(Control):
    def __init__(self, program, azimuth):
        Control.__init__(self, 1)
        self.program = program
        self.azimuth = azimuth


class UPFGControl(Control):
    def __init__(self, target, major):
        Control.__init__(self, 3)
        self.target = target
        self.major = major


class CoastControl(Control):
    def __init__(self, length):
        Control.__init__(self, 5)
        self.length = length


class GrowingList(list):
    def __init__(self, default=None):
        self.default = default

    def __setitem__(self, index, value):
        if index >= len(self):
            self.extend([self.default]*(index + 1 - len(self)))
        list.__setitem__(self, index, value)

    def __getitem__(self, index):
        if index >= len(self):
            return self.default
        return list.__getitem__(self, index)


def flight_sim_3d(vehicle, stage, initial, control, jettison, dt, apply_guidance_func=None, get_state_func=None):
    """
    Complete 3DoF flight simulation in Cartesian coordinates.
    Vehicle is modelled as a point mass with drag. Simulation is located in an
    Earth-centered, inertial frame of reference, so the vehicle is not
    stationary even on the launch pad. However, the Earth itself does not
    rotate (so the ground tracks generated by this simulation will not be
    fully accurate). Atmosphere is modelled from RO data, but only direct drag
    effects are modelled (no AoA or lift).

    :param vehicle: Array of struct of type vehicle.
    :param stage: Integer identifying the currently flown stage (python
               notation, so stage==0 means the first element of vehicle array).
    :param initial: Struct of initial conditions type.
    :param control: Struct defining method of controlling the current stage.
        If UPFG is chosen, it will assume all further stages are to
        be either controlled by UPFG, or flied as coast phases.
    :param jettison: Minor jettison events. Array of size (n,2) or empty. List
        of jettison times (seconds since liftoff) in the first
        column, mass shed in each jettison event (kg) in the second.
    :param dt: Simulation precision in seconds. Each simulation step will
        last exactly that long. dt=0.1 gives decent enough results.
    :return: Struct containing all results of the simulation. Summary is
        directly in the struct, while detailed plots, orbit
        parameters and debug information are packed to respective
        substructs.
    """
    global mu  # Global variable, standard gravity parameter of the body;
               # gravity constant * mass of the body (kg).
    global g0  # Global variable, standard gravity acceleration (m/s).
    global R   # Global variable, radius of the body (m).
    global atmpressure  # Global variable, atmospheric pressure as a function of
                        # altitude; array of size (n,2), altitude (kilometres above
                        # sea level) in first column, pressure (atmospheres) in second.
    global atmtemperature  # Global variable, atmospheric temperature as a function
                           # of altitude; array of size (n,2), altitude (kilometres above
                           # sea level) in first column, temperature (Kelvins) in second.
    global period  # Global variable, period of Earth's rotation (seconds).
    
    # VEHICLE UNPACK
    mode = vehicle[stage].mode
    m = vehicle[stage].m0
    gLim = vehicle[stage].gLim
    engines = vehicle[stage].engines
    area = vehicle[stage].area
    drag = vehicle[stage].drag
    
    # DETERMINE SIMULATION LENGTH
    # Set a desired simulation length for a coast phase. For powered phases,
    # set maximum phase burn time. If a phase cuts off before running out of
    # fuel, all results will be trimmed to actual phase duration.
    if control.type == 5:
        maxT = control.length
    else:
        maxT = vehicle[stage].maxT
    
    # SIMULATION SETUP
    # n = int(np.floor(maxT/dt)+1)    # simulation steps
    t = GrowingList(0)              # simulation time
    F = GrowingList(0)              # thrust magnitude [N]
    acc = GrowingList(0)            # acceleration due to thrust magnitude [m/s^2]
    q = GrowingList(0)              # dynamic pressure [Pa]
    pitch = GrowingList(0)          # pitch command log [deg] (0 - straight up)
    yaw = GrowingList(0)            # yaw command log [deg] (0 - straight East, 90 - North)
    g_loss = 0                      # gravity d-v losses [m/s]
    d_loss = 0                      # drag d-v losses [m/s]
    # vehicle position in cartesian XYZ frame
    r = GrowingList(np.array([0, 0, 0]))     # from Earth's center [m]
    rmag = GrowingList(0)                    # magnitude [m]
    # vehicle velocity
    v = GrowingList(np.array([0, 0, 0]))     # relative to Earth's center [m/s]
    vmag = GrowingList(0)                    # magnitude [m/s]
    vy = GrowingList(0)                      # magnitude - altitude change [m/s]
    vt = GrowingList(0)                      # magnitude - tangential [m/s]
    vair = GrowingList(np.array([0, 0, 0]))  # relative to surface [m/s]
    vairmag = GrowingList(0)                 # magnitude relative to surface [m/s]
    # reference frame matrices
    nav = [np.array([0, 0, 0])]*3  # KSP-style navball frame (radial, North, East)
    rnc = [np.array([0, 0, 0])]*3  # PEG-style tangential frame (radial, normal, circumferential)
    # flight angles
    ang_p_srf = GrowingList(0)     # flight pitch angle, surface related
    ang_y_srf = GrowingList(0)     # flight yaw angle, surface related
    ang_p_obt = GrowingList(0)     # flight pitch angle, orbital (absolute)
    ang_y_obt = GrowingList(0)     # flight yaw angle, orbital (absolute)
    dbg = None
    upfg_internal = None
    # SIMULATION INITIALIZATION
    if isinstance(initial, LaunchSite):     # launch from static site
        r[0] = sph2cart(np.deg2rad(initial.longitude), np.deg2rad(initial.latitude), R+initial.altitude)
        nav = get_navball_frame(r[0])
        v[0] = surf_speed(r[0], nav)
    elif isinstance(initial, InitStruct):   # vehicle already in flight
        t[0] = initial.t
        r[0] = initial.r
        v[0] = initial.v
    else:
        print('Wrong initial conditions!')
        return None
    rmag[0] = np.linalg.norm(r[0])
    vmag[0] = np.linalg.norm(v[0])
    nav = get_navball_frame(r[0])
    rnc = get_circum_frame(r[0], v[0])
    vair[0] = v[0] - surf_speed(r[0], nav)
    vairmag[0] = max(np.linalg.norm(vair[0]), 1)
    vy[0] = np.vdot(v[0], nav[0])
    vt[0] = np.vdot(v[0], rnc[2])
    ang_p_srf[0] = get_angle_from_frame(vair[0], nav, 'pitch')
    ang_y_srf[0] = get_angle_from_frame(vair[0], nav, 'yaw')
    ang_p_obt[0] = get_angle_from_frame(v[0], nav, 'pitch')
    ang_y_obt[0] = get_angle_from_frame(v[0], nav, 'yaw')
    # the following 3 lines initialize acceleration plot
    p = approx_from_curve((rmag[0]-R)/1000, atmpressure)
    temp, _, _ = get_thrust(engines, p, t[0])
    acc[0] = temp / m
    eng = 1     # engine state flag (other value signifies some error):
                # 0 - fuel deprived;
                # 1 - running;
                # 2 - cut as scheduled by UPFG
                # 3 - cut exceptionally by a velocity limit
    
    # CONTROL SETUP
    if control.type == 0:       # natural gravity turn
        gtiP = control.pitch    # initial pitchover angle for gravity turn
        gtiV = control.velocity # velocity at which the pitchover begins
        azim = control.azimuth  # launch azimuth
        GT = 0  # gravity turn status flag:
                # 0 - not begun yet;
                # 1 - equaling to flight angle;
                # 2 - match flight angle
    elif control.type == 1:     # pitch program control, constant azimuth
        prog = control.program
        azim = control.azimuth
    elif control.type == 2:     # deprecated PEG mode
        print('Powered Explicit Guidance mode for 3D simulation is deprecated! Use UPFG instead (type 3).')
        return None
    elif control.type == 3:     # Unified Powered Flight Guidance
        target = control.target
        ct = control.major      # cycle time (UPFG will be called that often)
        lc = 0                  # last call was that long ago
        # Create internal states for UPFG - continue from last stage if
        # possible. First create physical state and CSE state structs, as
        # well as the debug information container.
        upfg_state = upfg.State(t[0], m, r[0], v[0])
        cser = upfg.CSERState(0, 0, 0, 0, 0)
        dbg = debug_initializator(int(np.floor(maxT/ct)))
        # Then check if the stage continues an already started UPFG routine.
        # If so, verify if the routine contains necessary fields (just
        # checks for 'tgo', assuming an erroneous struct will be empty or
        # contain just garbage).
        if isinstance(initial, InitStruct) and initial.upfg is not None \
            and isinstance(initial.upfg, upfg.UPEGState):
            # If the struct is okay, use it as initialization for the
            # current stage and reconverge UPFG. This helps avoid some
            # guidance oscillation after coast phases.
            upfg_internal = initial.upfg
            upfg_internal.tb = 0
            upfg_internal, guidance, debug = converge_upfg(vehicle[stage:len(vehicle)],
                                                           target, upfg_state, upfg_internal,
                                                           0, 50)
        # If no initial state was found, a new one must be built and converged.
        if upfg_internal is None:
            # Guidance initialization: project initial position direction unit
            # vector onto target plane, rotate with Rodrigues' formula about
            # 20 degrees prograde and extend to target length, finally calculate
            # velocity at this point.
            rdinit = rodrigues(unit(r[0]), -target.normal, 20)
            rdinit = rdinit * target.radius
            vdinit = target.velocity*unit(np.cross(-target.normal, rdinit))
            vdinit = vdinit - v[0]
            upfg_internal = upfg.UPEGState(cser, np.array([0, 0, 0]), rdinit, 
                                           -(mu/2)*r[0]/np.linalg.norm(r[0])**3,
                                           0, t[0], 0, v[0], vdinit)
            upfg_internal, guidance, debug = converge_upfg(vehicle[stage:len(vehicle)],
                                                           target, upfg_state, upfg_internal,
                                                           t[0], 50)
        dbg = debug_aggregator(dbg, debug)
        pitch[0] = guidance.pitch
        yaw[0] = guidance.yaw
    elif control.type == 5:    # coast phase (unguided free flight)
        acc[0] = 0
        eng = -1
    
    # SIMULATION MAIN LOOP
    for i in xrange(1, 1000000):  # arbitrary limit to avoid exhausting memory
        # GUIDANCE
        if control.type == 0:    # natural gravity turn
            # First if-set controls current state - initial is GT==0 which
            # means vehicle is going straight up, building speed. GT==1
            # means it's going fast enough to start pitching over in the
            # given direction OR that it already reached max allowed pitch
            # and is waiting for velocity vector to align with it. GT==2
            # means velocity vector has aligned and vehicle will hold the
            # prograde direction.
            if vy[i-1] >= gtiV and GT == 0:
                GT = 1
            elif ang_p_srf[i-1] > gtiP and GT == 1:
                GT = 2
            # Second if-set controls what to do depending on current state.
            # For GT==0 do nothing, just go straight up. For GT==1 pitch
            # over to the given angle at a constant rate of 1deg/s, hold the
            # given pitch after reaching it. For GT==2 just hold prograde.
            if GT == 0:
                pitch[i] = 0
                yaw[i] = azim
            elif GT == 1:
                pitch[i] = min(pitch[i-1]+dt, gtiP)
                yaw[i] = azim
            else:
                pitch[i] = ang_p_srf[i-1]
                yaw[i] = azim
        elif control.type == 1:  # pitch program control, constant azimuth
            pitch[i] = approx_from_curve(t[i-1], prog)
            yaw[i] = azim
        elif control.type == 3:  # Unified Powered Flight Guidance
            # Check if the current stage ran out of fuel (ie. if the current
            # phase exceeded its maximum burn time).
            if t[i-1]-t[0] > maxT and eng > 0:
                eng = 0
                break
            # Check if it's time for a UPFG call, increment time since the
            # last call if it's not.
            if lc < ct-dt:
                lc = lc + dt
            else:
                # Update struct holding the vehicle's physical state
                upfg_state.time     = t[i-1]
                upfg_state.mass     = m
                upfg_state.radius   = r[i-1]
                upfg_state.velocity = v[i-1]
                # Call UPFG and collect the debug output
                upfg_internal, guidance, debug = upfg.unified_powered_flight_guidance(
                               vehicle[stage:len(vehicle)],
                               target, upfg_state, upfg_internal)
                dbg = debug_aggregator(dbg, debug)
                # The following 6 lines were meant to handle UPFG divergence
                # handling, but this proves to be not straightforward and is
                # as of now a very low priority TODO.
                if dbg is not None and dbg.diverge[dbg.this-1] and not dbg.diverge[dbg.this-2]:
                    print('UPFG started to diverge at t+%f', t[i-1])
                if dbg is not None and not dbg.diverge[dbg.this-1] and dbg.diverge[dbg.this-2]:
                    dbg.diverge[dbg.this-1] = 1
                # Reset the last call timer
                lc = 0
            # Check if it's time for UPFG-predicted cutoff.
            if guidance.tgo-lc < dt and eng == 1:
                eng = 2
                break
            # Safety cutoff in case UPFG went crazy. Will cut off when
            # absolute target velocity is reached.
            if np.linalg.norm(v[i-1]) >= target.velocity:
                eng = 3
                break
            # Update current pitch and yaw commands (TODO: incorporate angle
            # rates here).
            pitch[i] = guidance.pitch
            yaw[i] = guidance.yaw

        # PHYSICS
        # Crash detection (10 meter tolerance - watch out for launch sites in depressions!)
        if rmag[i-1] <= R-10:
            eng = -100
            break
        desired_throttle = 0.0
        # Thrust: zero for coast flight, different calculations for constant
        # thrust and constant acceleration modes.
        p = approx_from_curve((rmag[i-1]-R)/1000, atmpressure)
        if control.type == 5:
            F[i] = 0
            dm = 0
        else:
            # Calculate default 100% thrust and adjust for constant acceleration mode if necessary
            F[i], dm, _ = get_thrust(engines, p, t[i-1])
            if mode == 2:  # Constant acceleration mode
                desired_thrust = gLim*g0 * m
                desired_throttle = desired_thrust/F[i]
                desired_throttle = min(desired_throttle, engines[0].data[1])  # minimum throttle clamp
                desired_throttle = max(desired_throttle, engines[0].data[0])  # maximum throttle clamp
                F[i] = F[i] * desired_throttle
                dm = dm * desired_throttle
            else:
                desired_throttle = 1.0
        acc[i] = F[i]/m
        acv = acc[i]*make_vector(nav, pitch[i], yaw[i])
        # gravity
        G = mu*r[i-1]/rmag[i-1]**3                    # acceleration [m/s^2]
        g_loss = g_loss + np.linalg.norm(G)*dt        # integrate gravity losses
        # drag
        cd = approx_from_curve(vairmag[i-1], drag)    # drag coefficient
        temp = approx_from_curve((rmag[i-1]-R)/1000, atmtemperature)+273.15
        dens = calculate_air_density(p*101325, temp)  # constant is pascals per atm
        q[i] = 0.5*dens*vairmag[i-1]**2               # dynamic pressure
        D = area*cd*q[i]/m                            # drag-induced acceleration [m/s^2]
        d_loss = d_loss + D*dt                        # integrate drag losses
        if get_state_func is not None:
            # Get the actual values, instead of predicting them
            r[i], v[i], m, t[i] = get_state_func()
        else:
            # Forecast values
            v[i] = v[i-1] + acv*dt - G*dt - D*unit(vair[i-1])*dt
            r[i] = r[i-1] + v[i]*dt
            m = m - dm*dt
            t[i] = t[i-1] + dt
        # absolute velocities
        vmag[i] = np.linalg.norm(v[i])
        vy[i] = np.vdot(v[i], nav[0])
        vt[i] = np.vdot(v[i], rnc[2])
        # position
        rmag[i] = np.linalg.norm(r[i])
        # local reference frames
        nav = get_navball_frame(r[i])
        rnc = get_circum_frame(r[i], v[i])
        # surface velocity (must be here because needs reference frames)
        vair[i] = v[i] - surf_speed(r[i], nav)
        vairmag[i] = np.linalg.norm(vair[i])
        # angles
        ang_p_srf[i] = get_angle_from_frame(vair[i], nav, 'pitch')
        ang_y_srf[i] = get_angle_from_frame(vair[i], nav, 'yaw')
        ang_p_obt[i] = get_angle_from_frame(v[i], nav, 'pitch')
        ang_y_obt[i] = get_angle_from_frame(v[i], nav, 'yaw')
        # MASS&TIME
        # Handle minor jettison events, if there are any.
        js = len(jettison)
        if js > 0:
            for j in range(js):
                # For each event check whether it hasn't been scheduled for
                # a previous burn phase (its time set before the current
                # phase begun). If this isn't checked, all older jettisons
                # would be repeated in this phase.
                # Then check whether it is already time to execute the event.
                # Reduce the vehicle's mass by scheduled amount and set the
                # event's time to negative (so that it never passes the
                # first check again).
                if jettison[j][0] < t[0]:
                    continue
                elif jettison[j][0] <= t[i]:
                    m = m - jettison[j][1]
                    jettison[j][0] = -1

        # Added for integration with kRPC
        if apply_guidance_func is not None:
            apply_guidance_func(pitch[i], yaw[i], desired_throttle)
        # Computed dt instead of being constant
        dt = t[i] - t[i-1]
        # Updated break condition instead of limited steps
        if t[i]-t[0] > maxT:
            break

    # OUTPUT
    # Trim all outputs for the actual duration of the flight.
    plots = PlotEntry(t[0:i], r[0:i], rmag[0:i], v[0:i], vy[0:i], vt[0:i], vmag[0:i], 
                      F[0:i], acc[0:i], q[0:i], pitch[0:i], yaw[0:i], vair[0:i], vairmag[0:i],
                      ang_p_srf[0:i], ang_y_srf[0:i], ang_p_obt[0:i], ang_y_obt[0:i])
    # Add debug data if it was created, add a dummy struct otherwise (see
    # below comment on UPFG persistence).
    if dbg is not None:
        plots.debug = dbg
    else:
        plots.debug = {}
    orbit = Orbit(0, 0, 0, 0, 0, 0)
    results = Result((rmag[i-1]-R)/1000, 0, 0, orbit, vmag[i-1], np.vdot(v[i-1], nav[0]), np.vdot(v[i-1], rnc[2]), 
                     0, 0, g_loss, d_loss, g_loss+d_loss, maxT-t[i-1]+t[0], plots, eng)
    # Handle UPFG state persistence between stages. Turns out it is CRUCIAL
    # for multistage guidance capability.
    # For guided stages, store final UPFG internal state in the results
    # struct. For unguided ones, save the state passed to the simulation
    # initialization struct - this allows handling coasting between guided
    # stages by resultsToInit (guided stage returns UPFG state, coast stage
    # simply copies it, next guided stage continues from that state).
    # To allow stacking structs in an array, all of them must have the same
    # set of fields, so in case no UPFG was ever called in a stage, a dummy
    # is created.
    if upfg_internal is not None:
        results.upfg = upfg_internal
    elif isinstance(initial, InitStruct) and initial.upfg is not None:
        results.upfg = initial.upfg
    else:
        results.upfg = {}
    results.apoapsis, results.periapsis, results.orbit.sma, \
        results.orbit.ecc, results.orbit.inc, \
        results.orbit.lan, results.orbit.aop, \
        results.orbit.tan = get_orbital_elements(r[i-1], v[i-1])
    # Get time and value of maxQ, format time to seconds.
    results.max_Qt, results.max_Qv = get_max_value(q)
    results.max_Qt = t[results.max_Qt]
    return results


# constructs a local reference frame, KSP-navball style
def get_navball_frame(r):
    # pass current position under r (1x3)
    up = unit(r)                       # true Up direction (radial away from Earth)
    east = np.cross(np.array([0, 0, 1]), up)  # true East direction
    north = np.cross(up, east)              # true North direction (completes frame)
    f = [None]*3
    # return a right-handed coordinate system base
    f[0] = up
    f[1] = unit(north)
    f[2] = unit(east)
    return f


# constructs a local reference frame in style of PEG coordinate base
def get_circum_frame(r, v):
    # pass current position under r (1x3)
    # current velocity under v (1x3)
    radial = unit(r)                   # Up direction (radial away from Earth)
    normal = unit(np.cross(r, v))      # Normal direction (perpendicular to orbital plane)
    circum = np.cross(normal, radial)  # Circumferential direction (tangential to sphere, in motion plane)
    f = [None]*3
    # return a left(?)-handed coordinate system base
    f[0] = radial
    f[1] = normal
    f[2] = circum
    return f


# finds rotation angle between the two frames
def rnc2nav(rnc, nav):
    # pass reference frame matrices
    # by their definitions, their 'radial' component is the same, therefore
    # rotation between them can be described with a single number
    alpha = np.vdot(rnc[2], nav[2])
    return alpha


# constructs a unit vector in the global frame for a given pitch and yaw
# understanding frame as a 3x3 matrix of vectors 'up', 'north', 'east',
# rotates the 'up' vector towards the 'east' by 'p' degrees (pitch), and
# then rotates this about the 'up' axis by 'y' degrees towards 'north' (yaw)
def make_vector(frame, p, y):
    v = rodrigues(frame[0], frame[1], p)
    v = rodrigues(v, frame[0], y)
    return v


# finds Earth's rotation velocity vector at given cartesian location
def surf_speed(r, nav):
    global R
    global period
    _, lat, _ = cart2sph(r[0], r[1], r[2])
    vel = 2*np.pi*R/period * np.cos(lat)
    rot = vel*nav[2]  # third componend is East vector
    return rot


# initializes UPFG debug data aggregator with zero vectors of appropriate sizes
# pass expected length of the vector (number of guidance iterations, usually
# maxT / guidance cycle + 5 should be okay)
def debug_initializator(n):
    return upfg.DebugState(0, GrowingList(0), GrowingList([0]*4), GrowingList([0]*4), GrowingList(0),
                           GrowingList([0]*4), GrowingList([0]*4),
                           GrowingList(0), GrowingList(0), GrowingList(0), GrowingList(0), GrowingList(0),
                           GrowingList(0), GrowingList(0), GrowingList(0),
                           GrowingList([0]*4), GrowingList([0]*4), GrowingList([0]*4), GrowingList([0]*4),
                           GrowingList([0]*4), GrowingList(0),
                           GrowingList([0]*4), GrowingList(0), GrowingList([0]*4),
                           GrowingList([0]*4), GrowingList(0), GrowingList(0), GrowingList([0]*4), GrowingList([0]*4),
                           GrowingList([0]*4), GrowingList([0]*4), GrowingList(0), GrowingList([0]*4), GrowingList(0),
                           GrowingList([0]*4), GrowingList([0]*4), GrowingList([0]*4) ,GrowingList([0]*4),
                           GrowingList(0), GrowingList(0), GrowingList(0), GrowingList(0), GrowingList(0),
                           GrowingList([0]*4), GrowingList([0]*4), GrowingList([0]*4), GrowingList([0]*4),
                           GrowingList([0]*4), GrowingList([0]*4), GrowingList([0]*4), GrowingList([0]*4),
                           GrowingList([0]*4), GrowingList([0]*4), GrowingList(0))

# handles UPFG debug data aggregating
# adds debug data from a single guidance iteration into aggregated, time-based
# struct of vectors
# pass initialized debug structure and UPFG debug output
def debug_aggregator(a, d):
    # we must know where to put the new results
    i = a.this
    a.this = i+1
    # and onto the great copy...
    a.time[i] = d.time
    a.r[i][0:3] = d.r
    a.r[i][3] = np.linalg.norm(d.r)
    a.v[i][0:3] = d.v
    a.v[i][3] = np.linalg.norm(d.v)
    a.m[i] = d.m
    a.dvsensed[i][0:3] = d.dvsensed
    a.dvsensed[i][3] = np.linalg.norm(d.dvsensed)
    a.vgo1[i][0:3] = d.vgo1
    a.vgo1[i][3] = np.linalg.norm(d.vgo1)
    a.L1[i] = d.L1
    a.tgo[i] = d.tgo
    a.L[i] = d.L
    a.J[i] = d.J
    a.S[i] = d.S
    a.Q[i] = d.Q
    a.P[i] = d.P
    a.H[i] = d.H
    a.lambda_vec[i][0:3] = d.lambda_vec
    a.lambda_vec[i][3] = np.linalg.norm(d.lambda_vec)
    a.rgrav1[i][0:3] = d.rgrav1
    a.rgrav1[i][3] = np.linalg.norm(d.rgrav1)
    a.rgo1[i][0:3] = d.rgo1
    a.rgo1[i][3] = np.linalg.norm(d.rgo1)
    a.iz1[i][0:3] = d.iz1
    a.iz1[i][3] = np.linalg.norm(d.iz1)
    a.rgoxy[i][0:3] = d.rgoxy
    a.rgoxy[i][3] = np.linalg.norm(d.rgoxy)
    a.rgoz[i] = d.rgoz
    a.rgo2[i][0:3] = d.rgo2
    a.rgo2[i][3] = np.linalg.norm(d.rgo2)
    a.lambdade[i] = d.lambdade
    a.lambdadot[i][0:3] = d.lambdadot
    a.lambdadot[i][3] = np.linalg.norm(d.lambdadot)
    a.iF[i][0:3] = d.iF
    a.iF[i][3] = np.linalg.norm(d.iF)
#    a.phi(i) = d.phi;
#    a.phidot(i) = d.phidot;
#    a.vthrust(i,1:3) = d.vthrust;
#    a.vthrust(i,4) = norm(d.vthrust);
#    a.rthrust(i,1:3) = d.rthrust;
#    a.rthrust(i,4) = norm(d.rthrust);
#    a.vbias(i,1:3) = d.vbias;
#    a.vbias(i,4) = norm(d.vbias);
#    a.rbias(i,1:3) = d.rbias;
#    a.rbias(i,4) = norm(d.rbias);
#    a.pitch(i) = d.pitch;
#    a.EAST(i,1:3) = d.EAST;
#    a.EAST(i,4) = norm(d.EAST);
#    a.yaw(i) = d.yaw;
#    a.rc1(i,1:3) = d.rc1;
#    a.rc1(i,4) = norm(d.rc1);
#    a.vc1(i,1:3) = d.vc1;
#    a.vc1(i,4) = norm(d.vc1);
#    a.rc2(i,1:3) = d.rc2;
#    a.rc2(i,4) = norm(d.rc2);
#    a.vc2(i,1:3) = d.vc2;
#    a.vc2(i,4) = norm(d.vc2);
#    a.cser_dtcp(i) = d.cser.dtcp;
#    a.cser_xcp(i) = d.cser.xcp;
#    a.cser_A(i) = d.cser.A;
#    a.cser_D(i) = d.cser.D;
#    a.cser_E(i) = d.cser.E;
#    a.vgrav(i,1:3) = d.vgrav;
#    a.vgrav(i,4) = norm(d.vgrav);
#    a.rgrav2(i,1:3) = d.rgrav2;
#    a.rgrav2(i,4) = norm(d.rgrav2);
#    a.rp(i,1:3) = d.rp;
#    a.rp(i,4) = norm(d.rp);
#    a.rd(i,1:3) = d.rd;
#    a.rd(i,4) = norm(d.rd);
#    a.ix(i,1:3) = d.ix;
#    a.ix(i,4) = norm(d.ix);
#    a.iz2(i,1:3) = d.iz2;
#    a.iz2(i,4) = norm(d.iz2);
#    a.vd(i,1:3) = d.vd;
#    a.vd(i,4) = norm(d.vd);
#    a.vgop(i,1:3) = d.vgop;
#    a.vgop(i,4) = norm(d.vgop);
#    a.dvgo(i,1:3) = d.dvgo;
#    a.dvgo(i,4) = norm(d.dvgo);
#    a.vgo2(i,1:3) = d.vgo2;
#    a.vgo2(i,4) = norm(d.vgo2);
    a.diverge[i] = d.diverge
    return a

# handles UPFG convergence by running it in loop until tgo stabilizes
def converge_upfg(vehicle, target, state, internal, time, max_iters):
    global convergence_criterion
    fail = 1
    internal, guidance, debug = upfg.unified_powered_flight_guidance(vehicle, target, state, internal)
    for i in range(max_iters):
        t1 = internal.tgo
        internal, guidance, debug = upfg.unified_powered_flight_guidance(vehicle, target, state, internal)
        t2 = internal.tgo
        if abs((t1-t2)/t1) < convergence_criterion:
            if time > 0:
                print('UPFG converged after %d iterations, predicted insertion time: T+%.1fs (tgo=%.1f).' % (i, time+t2, t2))
            fail = 0
            break
    if fail:
        print('UPFG failed to converge in %d iterations!' % i)
    return internal, guidance, debug
