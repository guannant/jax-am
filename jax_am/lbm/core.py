import numpy as onp
import jax.numpy as np
import jax
import meshio
import os
import time

from jax_am.common import box_mesh
from jax_am.lbm.utils import ST, compute_cell_centroid, clean_sols, shape_wrapper, to_id_xyz

# jax.config.update("jax_enable_x64", True)
# jax.config.update("jax_debug_nans", True)


def simulation(lbm_args, data_dir, meshio_mesh, initial_phase, fluid_only=False):
    def extract_7x3x3_grid(lattice_id, values):
        id_x, id_y, id_z = to_id_xyz(lattice_id, lbm_args)
        grid_index = np.ix_(np.array([(id_x - 3) % Nx, (id_x - 2) % Nx, (id_x - 1) % Nx, id_x, (id_x + 1) % Nx, (id_x + 2) % Nx, (id_x + 3) % Nx]), 
                            np.array([(id_y - 1) % Ny, id_y, (id_y + 1) % Ny]),
                            np.array([(id_z - 1) % Nz, id_z, (id_z + 1) % Nz]))
        grid_values = values[grid_index]
        return grid_values 

    def extract_3x7x3_grid(lattice_id, values):
        id_x, id_y, id_z = to_id_xyz(lattice_id, lbm_args)
        grid_index = np.ix_(np.array([(id_x - 1) % Nx, id_x, (id_x + 1) % Nx]),  
                            np.array([(id_y - 3) % Ny, (id_y - 2) % Ny, (id_y - 1) % Ny, id_y, (id_y + 1) % Ny, (id_y + 2) % Ny, (id_y + 3) % Ny]),
                            np.array([(id_z - 1) % Nz, id_z, (id_z + 1) % Nz]))
        grid_values = values[grid_index]
        return grid_values 

    def extract_3x3x7_grid(lattice_id, values):
        id_x, id_y, id_z = to_id_xyz(lattice_id, lbm_args)
        grid_index = np.ix_(np.array([(id_x - 1) % Nx, id_x, (id_x + 1) % Nx]), 
                            np.array([(id_y - 1) % Ny, id_y, (id_y + 1) % Ny]),
                            np.array([(id_z - 3) % Nz, (id_z - 2) % Nz, (id_z - 1) % Nz, id_z, (id_z + 1) % Nz, (id_z + 2) % Nz, (id_z + 3) % Nz]))
        grid_values = values[grid_index]
        return grid_values  

    def extract_3x3x3_grid(lattice_id, values):
        id_x, id_y, id_z = to_id_xyz(lattice_id, lbm_args)
        grid_index = np.ix_(np.array([(id_x - 1) % Nx, id_x, (id_x + 1) % Nx]), 
                            np.array([(id_y - 1) % Ny, id_y, (id_y + 1) % Ny]),
                            np.array([(id_z - 1) % Nz, id_z, (id_z + 1) % Nz]))
        grid_values = values[grid_index]
        return grid_values        

    def extract_local(lattice_id, values):
        id_x, id_y, id_z = to_id_xyz(lattice_id, lbm_args)
        return values[vels[0] + id_x, vels[1] + id_y, vels[2] + id_z]

    def extract_income(lattice_id, values):
        id_x, id_y, id_z = to_id_xyz(lattice_id, lbm_args)
        return values[vels[0] + id_x, vels[1] + id_y, vels[2] + id_z, rev]

    def extract_self(lattice_id, values):
        id_x, id_y, id_z = to_id_xyz(lattice_id, lbm_args)
        return values[id_x, id_y, id_z]


    def equilibrium_f(q, rho, u):
        vel_dot_u = np.sum(vels[:, q] * u)
        u_sq = np.sum(u * u)
        return weights[q] * rho * (1. + vel_dot_u/cs_sq + vel_dot_u**2./(2.*cs_sq**2.) - u_sq/(2.*cs_sq))

    equilibrium_f_vmap = jax.vmap(equilibrium_f, in_axes=(0, None, None))


    def equilibrium_h(q, enthalpy, T, u):
        u_sq = np.sum(u * u)
        vel_dot_u = np.sum(vels[:, q] * u)
        result = weights[q]*heat_capacity*T*(1. + vel_dot_u/cs_sq + vel_dot_u**2./(2.*cs_sq**2.) - u_sq/(2.*cs_sq))
        result0 = enthalpy - heat_capacity*T + weights[0]*heat_capacity*T*(1 - u_sq/(2.*cs_sq))
        return np.where(q == 0, result0, result)

    equilibrium_h_vmap = jax.vmap(equilibrium_h, in_axes=(0, None, None, None))


    def f_forcing(q, u, volume_force):
        ei = vels[:, q]
        return (1. - 1./(2.*tau_viscosity_nu)) * weights[q] * np.sum(((ei - u)/cs_sq + np.sum(ei * u)/cs_sq**2 * ei) * volume_force)

    f_forcing_vmap = jax.vmap(f_forcing, in_axes=(0, None, None))


    def h_forcing_vmap(volume_power, rho):
        return volume_power / rho * weights


    @jax.jit
    def compute_rho(f_distribute):
        rho = np.sum(f_distribute, axis=-1)
        return rho

    @jax.jit
    def compute_enthalpy(h_distribute):
        enthalpy = np.sum(h_distribute, axis=-1)
        return enthalpy

    @jax.jit
    def compute_T(enthalpy):
        T = np.where(enthalpy < enthalpy_s, enthalpy/heat_capacity, 
            np.where(enthalpy < enthalpy_l, T_solidus + (enthalpy - enthalpy_s)/(enthalpy_l - enthalpy_s)*(T_liquidus - T_solidus), 
                                            T_liquidus + (enthalpy - enthalpy_l)/heat_capacity))
        return T

    @jax.jit
    def compute_vof(rho, phase, mass):
        vof = np.where(phase == ST.LIQUID, rho, mass)
        vof = np.where(phase == ST.GAS, 0., vof)
        vof = np.where(phase == ST.WALL, rho0, vof)
        return vof

    def compute_curvature(lattice_ids, vof):
        def get_phi(lattice_id, vof):
            # TODO: phi near obstacle should never be used
            vof_grid = extract_3x3x3_grid(lattice_id, vof) # (3, 3, 3)
            c_id = 1
            phi_x = (vof_grid[c_id + 1, c_id, c_id] - vof_grid[c_id - 1, c_id, c_id])/(2.*h)
            phi_y = (vof_grid[c_id, c_id + 1, c_id] - vof_grid[c_id, c_id - 1, c_id])/(2.*h)
            phi_z = (vof_grid[c_id, c_id, c_id + 1] - vof_grid[c_id, c_id, c_id - 1])/(2.*h)
            return np.array([phi_x, phi_y, phi_z])

        get_phi_vmap = jax.jit(jax.vmap(get_phi, in_axes=(0, None)))

        def kappa_7x3x3(lattice_id, vof):
            vof_grid = extract_7x3x3_grid(lattice_id, vof) # (7, 3, 3)
            return np.sum(vof_grid, axis=0)

        hf_7x3x3_vmap = jax.jit(jax.vmap(kappa_7x3x3, in_axes=(0, None)))

        def kappa_3x7x3(lattice_id, vof):
            vof_grid = extract_3x7x3_grid(lattice_id, vof) # (3, 7, 3)
            return np.sum(vof_grid, axis=1)

        hf_3x7x3_vmap = jax.jit(jax.vmap(kappa_3x7x3, in_axes=(0, None)))

        def kappa_3x3x7(lattice_id, vof):
            vof_grid = extract_3x3x7_grid(lattice_id, vof) # (3, 3, 7)
            return np.sum(vof_grid, axis=2)

        hf_3x3x7_vmap = jax.jit(jax.vmap(kappa_3x3x7, in_axes=(0, None)))

        def curvature(hgt_func):
            c_id = 1
            Hx = (hgt_func[..., c_id + 1, c_id] - hgt_func[..., c_id - 1, c_id])/(2.*h)
            Hy = (hgt_func[..., c_id, c_id + 1] - hgt_func[..., c_id, c_id - 1])/(2.*h)
            Hxx = (hgt_func[..., c_id + 1, c_id] - 2.*hgt_func[..., c_id, c_id] + hgt_func[..., c_id - 1, c_id])/h**2
            Hyy = (hgt_func[..., c_id, c_id + 1] - 2.*hgt_func[..., c_id, c_id] + hgt_func[..., c_id, c_id - 1])/h**2
            Hxy = (hgt_func[..., c_id + 1, c_id + 1] - hgt_func[..., c_id - 1, c_id + 1] - hgt_func[..., c_id + 1, c_id - 1] 
                 + hgt_func[..., c_id - 1, c_id - 1])/(4*h)
            kappa = -(Hxx + Hyy + Hxx*Hy**2. + Hyy*Hx**2. - 2.*Hxy*Hx*Hy) / (1 + Hx**2. + Hy**2.)**(3./2.)
            kappa = np.where(np.isfinite(kappa), kappa, 0.)
            return kappa

        kappa_1 = curvature(hf_7x3x3_vmap(lattice_ids, vof))
        kappa_2 = curvature(hf_3x7x3_vmap(lattice_ids, vof))
        kappa_3 = curvature(hf_3x3x7_vmap(lattice_ids, vof))

        phi = get_phi_vmap(lattice_ids, vof)
            
        kappa = np.where(np.logical_and(np.absolute(phi[..., 0]) >= np.absolute(phi[..., 1]), np.absolute(phi[..., 0]) >= np.absolute(phi[..., 2])), kappa_1, 
               np.where(np.logical_and(np.absolute(phi[..., 1]) >= np.absolute(phi[..., 0]), np.absolute(phi[..., 1]) >= np.absolute(phi[..., 2])), kappa_2, kappa_3))
        
        return phi, kappa

    compute_curvature = jax.jit(shape_wrapper(compute_curvature, lbm_args))

    def compute_T_grad(lattice_ids, T, phase):
        # TODO: duplicated code with VOF
        def get_T_grad(lattice_id, T, phase):
            T_grid = extract_3x3x3_grid(lattice_id, T) # (3, 3, 3)
            phase_grid = extract_3x3x3_grid(lattice_id, phase)
            c_id = 1
            T_grid = np.where(np.logical_or(phase_grid == ST.GAS, phase_grid == ST.WALL), T_grid[c_id, c_id, c_id], T_grid)
            T_grad_x = (T_grid[c_id + 1, c_id, c_id] - T_grid[c_id - 1, c_id, c_id])/(2.*h)
            T_grad_y = (T_grid[c_id, c_id + 1, c_id] - T_grid[c_id, c_id - 1, c_id])/(2.*h)
            T_grad_z = (T_grid[c_id, c_id, c_id + 1] - T_grid[c_id, c_id, c_id - 1])/(2.*h)
            return np.array([T_grad_x, T_grad_y, T_grad_z])
        get_T_grad_vmap = jax.jit(jax.vmap(get_T_grad, in_axes=(0, None, None)))
        T_grad = get_T_grad_vmap(lattice_ids, T, phase)
        return T_grad
 
    compute_T_grad = jax.jit(shape_wrapper(compute_T_grad, lbm_args))

    @jax.jit
    def compute_f_source_term(rho, vof, phi, kappa, T, T_grad):
        gravity_force = rho[:, :, :, None] * g[None, None, None, :]
        st_force = st_coeff * kappa[:, :, :, None] * phi
        normal = phi / np.linalg.norm(phi, axis=-1)[:, :, :, None]
        normal = np.where(np.isfinite(normal), normal, 0.)
        Marangoni_force = st_grad_coeff * (T_grad - np.sum(normal*T_grad, axis=-1)[:, :, :, None]*normal) * \
                          np.linalg.norm(phi, axis=-1)[:, :, :, None]*2.*vof[:, :, :, None]

        recoil_pressure = rp_coeff*p_atm*np.exp(latent_heat_evap*M0*(T - T_evap)/(gas_const*T*T_evap))[:, :, :, None] * phi
        
        source_term = gravity_force + st_force + Marangoni_force + recoil_pressure
        return source_term


    @jax.jit
    def compute_u(f_distribute, rho, T, f_source_term):
        u = (np.sum(f_distribute[:, :, :, :, None] * vels.T[None, None, None, :, :], axis=-2) + dt*m*f_source_term) / \
             np.where(rho == 0., 1., rho)[:, :, :, None]
        u = np.where((rho == 0.)[:, :, :, None], 0., u)
        u = np.where((T < T_solidus)[:, :, :, None], 0., u)
        return u


    def compute_h_src(lattice_id, T, vof, phi, cell_centroids, laser_x, laser_y, switch):
        T_self = extract_self(lattice_id, T)
        vof_self = extract_self(lattice_id, vof)
        phi_self = extract_self(lattice_id, phi)
        centroid = cell_centroids[lattice_id]

        q_rad = SB_const*emissivity*(T0**4 - T_self**4)
        q_conv = h_conv*(T0 - T_self)
        q_loss = np.linalg.norm(phi_self) * (q_conv + q_rad) * 2.*vof_self

        x, y, z = centroid

        # d = 1./4.*domain_z
        # q_laser = 2*laser_power*absorbed_fraction/(np.pi*beam_size**2)*np.exp(-2.*((x-laser_x)**2 + (y-laser_y)**2)/beam_size**2)
        # q_laser_body = q_laser/d * np.where(np.absolute(z - laser_z) < d, 1., 0.)  
        # heat_source = q_loss + q_laser_body

        q_laser = switch * 2*laser_power*absorbed_fraction/(np.pi*beam_size**2)*np.exp(-2.*((x-laser_x)**2 + (y-laser_y)**2)/beam_size**2)

        # heat_source = np.linalg.norm(phi_self) * q_laser * 2.*vof_self + q_loss # TODO: 2.*vof_self?

        tmp = (-phi_self * np.array([0., 0., 1.]))[2]
        tmp = np.where(tmp > 0., tmp, 0.)
        heat_source = tmp * q_laser * 2.*vof_self + q_loss

        return heat_source

    compute_h_source_term = jax.jit(shape_wrapper(jax.vmap(compute_h_src, in_axes=(0, None, None, None, None, None, None, None)), lbm_args))


    def collide_f(lattice_id, f_distribute, rho, T, u, phase, f_source_term):
        """Returns f_distribute
        """
        f_distribute_self = extract_self(lattice_id, f_distribute) 
        rho_self = extract_self(lattice_id, rho)
        u_self = extract_self(lattice_id, u)
        phase_local = extract_local(lattice_id, phase)
        T_self = extract_self(lattice_id, T)
        f_source_term_self = extract_self(lattice_id, f_source_term)

        def gas_or_wall():
            return np.zeros_like(f_distribute_self)

        def nongas():
            f_equil = equilibrium_f_vmap(np.arange(Ns), rho_self, u_self)  
            forcing = f_forcing_vmap(np.arange(Ns), u_self, f_source_term_self)

            new_f_dist = np.where(T_self < T_solidus, weights*rho_self,
                1./tau_viscosity_nu*(f_equil - f_distribute_self) + f_distribute_self + forcing*dt)

            # new_f_dist = 1./tau_viscosity_nu*(f_equil - f_distribute_self) + f_distribute_self + forcing*dt

            return new_f_dist

        return jax.lax.cond(np.logical_or(phase_local[0] == ST.GAS, phase_local[0] == ST.WALL), gas_or_wall, nongas)

    collide_f_vmap = jax.jit(shape_wrapper(jax.vmap(collide_f, in_axes=(0, None, None, None, None, None, None)), lbm_args))


    def collide_h(lattice_id, h_distribute, enthalpy, T, rho, u, phase, h_source_term):
        """Returns h_distribute
        """
        h_distribute_self = extract_self(lattice_id, h_distribute) 
        enthalpy_self = extract_self(lattice_id, enthalpy)
        T_self = extract_self(lattice_id, T)
        rho_self = extract_self(lattice_id, rho)
        u_self = extract_self(lattice_id, u)
        phase_local = extract_local(lattice_id, phase)

        h_source_term_self = extract_self(lattice_id, h_source_term)

        def gas_or_wall():
            return np.zeros_like(h_distribute_self)

        def nongas():
            h_equil = equilibrium_h_vmap(np.arange(Ns), enthalpy_self, T_self, u_self)
            heat_source = h_forcing_vmap(h_source_term_self, rho_self)   

            tau_diffusivity = np.where(T_self < T_solidus, tau_diffusivity_s, tau_diffusivity_l)
            new_h_dist = 1./tau_diffusivity*(h_equil - h_distribute_self) + h_distribute_self + heat_source*dt

            # new_h_dist = 1./tau_diffusivity_s*(h_equil - h_distribute_self) + h_distribute_self  

            return new_h_dist

        return jax.lax.cond(np.logical_or(phase_local[0] == ST.GAS, phase_local[0] == ST.WALL), gas_or_wall, nongas)

    collide_h_vmap = jax.jit(shape_wrapper(jax.vmap(collide_h, in_axes=(0, None, None, None, None, None, None, None)), lbm_args))


    def update_f(lattice_id, f_distribute, rho, u, phase, mass, vof):
        """Returns f_distribute, mass
        """
        f_distribute_self = extract_self(lattice_id, f_distribute) 
        f_distribute_income = extract_income(lattice_id, f_distribute)
        rho_self = extract_self(lattice_id, rho)
        u_self = extract_self(lattice_id, u)
        phase_local = extract_local(lattice_id, phase)
        vof_local = extract_local(lattice_id, vof)

        def gas_or_wall():
            return np.zeros_like(f_distribute_self), 0.

        def nongas():
            def stream_wall(q):
                return f_distribute_self[rev[q]]

            def stream_gas(q):
                return equilibrium_f(rev[q], rho_g, u_self) + equilibrium_f(q, rho_g, u_self) - f_distribute_self[rev[q]]

            def stream_liquid_or_lg(q):
                return f_distribute_income[rev[q]]

            def compute_stream(q):  
                return jax.lax.cond(phase_local[rev[q]] == ST.GAS, stream_gas, 
                    lambda q: jax.lax.cond(phase_local[rev[q]] == ST.WALL, stream_wall, stream_liquid_or_lg, q), q)

            streamed_f_dist = jax.vmap(compute_stream)(np.arange(Ns)) # (Ns,)

            def liquid():
                return streamed_f_dist, rho_self

            def lg():
                def mass_change_liquid(q):
                    f_in = f_distribute_income[q] 
                    f_out = f_distribute_self[q]
                    return f_in - f_out

                def mass_change_lg(q):
                    f_in = f_distribute_income[q]
                    f_out = f_distribute_self[q]
                    vof_in = vof_local[q]
                    vof_out = vof_local[0]
                    return (f_in - f_out) * (vof_in + vof_out) / 2.

                def mass_change_gas_or_wall(q):
                    return 0.

                def mass_change(q):
                    return jax.lax.switch(phase_local[q], [mass_change_liquid, mass_change_lg, mass_change_gas_or_wall, mass_change_gas_or_wall], q)

                delta_m = np.sum(jax.vmap(mass_change)(np.arange(Ns)))
                mass_self = extract_self(lattice_id, mass)
                return streamed_f_dist, delta_m + mass_self

            return jax.lax.cond(phase_local[0] == ST.LIQUID, liquid, lg)

        return jax.lax.cond(np.logical_or(phase_local[0] == ST.GAS, phase_local[0] == ST.WALL), gas_or_wall, nongas)

    update_f_vmap = jax.jit(shape_wrapper(jax.vmap(update_f, in_axes=(0, None, None, None, None, None, None)), lbm_args))



    def update_h(lattice_id, h_distribute, u, phase):
        """Returns h_distribute
        """
        h_distribute_self = extract_self(lattice_id, h_distribute) 
        h_distribute_income = extract_income(lattice_id, h_distribute)
        u_self = extract_self(lattice_id, u)
        phase_local = extract_local(lattice_id, phase)

        def gas_or_wall():
            return np.zeros_like(h_distribute_self)

        def nongas():
            def stream_wall(q):
                return equilibrium_h(q, T0*heat_capacity, T0, np.zeros(3))

            def stream_gas(q):
                return h_distribute_self[rev[q]]
 
            def stream_liquid_or_lg(q):
                return h_distribute_income[rev[q]]

            def compute_stream(q):  
                return jax.lax.cond(phase_local[rev[q]] == ST.GAS, stream_gas, 
                    lambda q: jax.lax.cond(phase_local[rev[q]] == ST.WALL, stream_wall, stream_liquid_or_lg, q), q)

            streamed_h_dist = jax.vmap(compute_stream)(np.arange(Ns)) # (Ns,)

            return streamed_h_dist

        return jax.lax.cond(np.logical_or(phase_local[0] == ST.GAS, phase_local[0] == ST.WALL), gas_or_wall, nongas)

    update_h_vmap = jax.jit(shape_wrapper(jax.vmap(update_h, in_axes=(0, None, None, None)), lbm_args))



    def reini_lg_to_liquid(lattice_id, f_distribute, phase, mass):
        """Returns phase
        """
        f_distribute_self = extract_self(lattice_id, f_distribute) # (Ns,)
        rho_self = np.sum(f_distribute_self) # (,)
        phase_self = extract_self(lattice_id, phase) # (,)
        mass_self = extract_self(lattice_id, mass) # (,)
        flag = np.logical_and(phase_self == ST.LG, mass_self > (1+theta)*rho_self)
        return jax.lax.cond(flag, lambda: ST.LIQUID, lambda: phase_self)
 
    reini_lg_to_liquid_vmap = jax.jit(shape_wrapper(jax.vmap(reini_lg_to_liquid, in_axes=(0, None, None, None)), lbm_args))

 
    def reini_gas_to_lg(lattice_id, f_distribute, h_distribute, rho, u, enthalpy, T, phase, mass):
        """Returns f_distribute, h_distribute, phase, mass
        """
        phase_local = extract_local(lattice_id, phase) # (Ns,)
        flag = np.logical_and(phase_local[0] == ST.GAS, np.any(phase_local[1:] == ST.LIQUID))
        def convert():
            rho_local = extract_local(lattice_id, rho) # (Ns,)
            u_local = extract_local(lattice_id, u) # (Ns, dim)
            enthalpy_local = extract_local(lattice_id, enthalpy) # (Ns,)
            T_local = extract_local(lattice_id, T) # (Ns,)
            nb_liquid_flag = np.logical_or(phase_local == ST.LIQUID, phase_local == ST.LG)
            rho_avg =  np.sum(nb_liquid_flag * rho_local) / np.sum(nb_liquid_flag)
            u_avg = np.sum(nb_liquid_flag[:, None] * u_local, axis=0) / np.sum(nb_liquid_flag)
            enthalpy_avg = np.sum(nb_liquid_flag * enthalpy_local) / np.sum(nb_liquid_flag)
            T_avg = np.sum(nb_liquid_flag * T_local) / np.sum(nb_liquid_flag)
            f_equil = equilibrium_f_vmap(np.arange(Ns), rho_avg, u_avg) # (Ns,

            h_equil = equilibrium_h_vmap(np.arange(Ns), enthalpy_avg, T_avg, u_avg)
            # h_equil = heat_capacity*T0*weights
            # h_equil = enthalpy_avg*weights

            return f_equil, h_equil, ST.LG, 0.
        def nonconvert():
            f_distribute_self = extract_self(lattice_id, f_distribute)
            h_distribute_self = extract_self(lattice_id, h_distribute)
            mass_self = extract_self(lattice_id, mass)
            return f_distribute_self, h_distribute_self, phase_local[0], mass_self
        return jax.lax.cond(flag, convert, nonconvert)

    reini_gas_to_lg_vmap = jax.jit(shape_wrapper(jax.vmap(reini_gas_to_lg, in_axes=(0, None, None, None, None, None, None, None, None)), lbm_args))


    def reini_lg_to_gas(lattice_id, f_distribute, phase, mass):
        """Returns phase
        """
        f_distribute_self = extract_self(lattice_id, f_distribute) # (Ns,)
        rho_self = np.sum(f_distribute_self) # (,)
        phase_self = extract_self(lattice_id, phase) # (,)
        mass_self = extract_self(lattice_id, mass) # (,)
        flag = np.logical_and(phase_self == ST.LG, mass_self < (0-theta)*rho_self)
        return jax.lax.cond(flag, lambda: ST.GAS, lambda: phase_self)
 
    reini_lg_to_gas_vmap = jax.jit(shape_wrapper(jax.vmap(reini_lg_to_gas, in_axes=(0, None, None, None)), lbm_args))


    def reini_liquid_to_lg(lattice_id, f_distribute, phase, mass):
        """Returns phase, mass
        """
        f_distribute_self = extract_self(lattice_id, f_distribute) # (Ns,)
        rho_self = np.sum(f_distribute_self) # (,)
        phase_local = extract_local(lattice_id, phase) # (Ns,)
        mass_self = extract_self(lattice_id, mass)
        flag = np.logical_and(phase_local[0] == ST.LIQUID, np.any(phase_local[1:] == ST.GAS))
        def convert():
            return ST.LG, rho_self
        def nonconvert():
            return phase_local[0], mass_self
        return jax.lax.cond(flag, convert, nonconvert)

    reini_liquid_to_lg_vmap = jax.jit(shape_wrapper(jax.vmap(reini_liquid_to_lg, in_axes=(0, None, None, None)), lbm_args))


    def adhoc_step(lattice_id, f_distribute, phase, mass):
        """Returns phase
        """
        f_distribute_self = extract_self(lattice_id, f_distribute) # (Ns,)
        rho_self = np.sum(f_distribute_self) # (,)
        phase_local = extract_local(lattice_id, phase) 
        mass_self = extract_self(lattice_id, mass)
        gas_nb_flag = np.all(np.logical_or(phase_local[1:] == ST.WALL, np.logical_or(phase_local[1:] == ST.GAS, phase_local[1:] == ST.LG)))
        gas_flag = np.logical_and(phase_local[0] == ST.LG, gas_nb_flag)
        liquid_nb_flag = np.all(np.logical_or(phase_local[1:] == ST.WALL, np.logical_or(phase_local[1:] == ST.LIQUID, phase_local[1:] == ST.LG)))
        liquid_flag = np.logical_and(phase_local[0] == ST.LG, liquid_nb_flag)
        return jax.lax.cond(gas_flag, lambda: ST.GAS, lambda: jax.lax.cond(liquid_flag, lambda: ST.LIQUID, lambda: phase_local[0]))

    adhoc_step_vmap = jax.jit(shape_wrapper(jax.vmap(adhoc_step, in_axes=(0, None, None, None)), lbm_args))


    def refresh_for_output(lattice_id, f_distribute, h_distribute, phase, mass):
        """Returns f_distribute, h_distribute, phase, mass
        """
        f_distribute_self = extract_self(lattice_id, f_distribute)
        h_distribute_self = extract_self(lattice_id, h_distribute)
        phase_self = extract_self(lattice_id, phase) # (Ns,)
        mass_self = extract_self(lattice_id, mass)
        def refresh():
            return np.zeros_like(f_distribute_self), np.zeros_like(h_distribute_self), phase_self, 0.
        def no_refresh():
            return f_distribute_self, h_distribute_self, phase_self, mass_self
        return jax.lax.cond(np.logical_or(phase_self == ST.GAS, phase_self == ST.WALL), refresh, no_refresh)

    refresh_for_output_vmap = jax.jit(shape_wrapper(jax.vmap(refresh_for_output, in_axes=(0, None, None, None, None)), lbm_args))


    def compute_total_mass(lattice_id, f_distribute, phase, mass):
        phase_self = extract_self(lattice_id, phase)
        def liquid():
            f_distribute_self = extract_self(lattice_id, f_distribute) # (Ns,)
            rho_self = np.sum(f_distribute_self) # (,)
            return rho_self
        def lg():
            mass_self = extract_self(lattice_id, mass)
            return mass_self
        def gas():
            return 0.
        return jax.lax.cond(phase_self == ST.LIQUID, liquid, lambda: jax.lax.cond(phase_self == ST.LG, lg, gas))

    compute_total_mass_vmap = jax.jit(shape_wrapper(jax.vmap(compute_total_mass, in_axes=(0, None, None, None)), lbm_args))


    def read_path():
        x_corners = lbm_args['laser_path']['x_pos']
        y_corners = lbm_args['laser_path']['y_pos']
        power_control = lbm_args['laser_path']['switch'][:-1]
        ts, xs, ys, zs, ps, mov_dir = [], [], [], [], [], []
        t_pre = 0.
        for i in range(len(x_corners) - 1):
            moving_direction = onp.array([x_corners[i + 1] - x_corners[i], 
                                          y_corners[i + 1] - y_corners[i]])
            traveled_dist = onp.linalg.norm(moving_direction)
            traveled_time = traveled_dist/lbm_args['scanning_vel']['value']
            ts_seg = onp.arange(t_pre, t_pre + traveled_time + 1e-10, lbm_args['dt']['value'])
            xs_seg = onp.linspace(x_corners[i], x_corners[i + 1], len(ts_seg))
            ys_seg = onp.linspace(y_corners[i], y_corners[i + 1], len(ts_seg))
            ps_seg = onp.linspace(power_control[i], power_control[i], len(ts_seg))
            ts.append(ts_seg)
            xs.append(xs_seg)
            ys.append(ys_seg)
            ps.append(ps_seg)
            t_pre = t_pre + traveled_time

        ts, xs, ys, ps = onp.hstack(ts), onp.hstack(xs), onp.hstack(ys), onp.hstack(ps) 
        return ts, xs, ys, ps


    def output_result(data_dir, meshio_mesh, f_distribute, h_distribute, phase, mass, kappa, melted, step):
        vtk_dir = os.path.join(data_dir, f'vtk')
        rho = np.sum(f_distribute, axis=-1) # (Nx, Ny, Nz)
        rho = np.where(rho == 0., 1., rho)
        u = np.sum(f_distribute[:, :, :, :, None] * vels.T[None, None, None, :, :], axis=-2) / rho[:, :, :, None]
        u = np.where(np.isfinite(u), u, 0.)
        u = np.where((phase == ST.LIQUID)[..., None], u, 0.)
        T = compute_T(compute_enthalpy(h_distribute))
        rho = rho * C_density
        u = u * C_length/C_time
        T = T * C_temperature
        max_x, max_y, max_z = to_id_xyz(np.argmax(T), lbm_args)
        print(f"max T = {np.max(T)} at ({max_x}, {max_y}, {max_z}) of ({Nx}, {Ny}, {Nz})")
        meshio_mesh.cell_data['phase'] = [onp.array(phase, dtype=onp.float32)]
        meshio_mesh.cell_data['mass'] = [onp.array(mass, dtype=onp.float32)]
        meshio_mesh.cell_data['rho'] = [onp.array(rho, dtype=onp.float32)]
        meshio_mesh.cell_data['kappa'] = [onp.array(kappa, dtype=onp.float32)]
        meshio_mesh.cell_data['vel'] = [onp.array(u.reshape(-1, 3) , dtype=onp.float32)]
        meshio_mesh.cell_data['T'] = [onp.array(T, dtype=onp.float32)]
        meshio_mesh.cell_data['melted'] = [onp.array(melted, dtype=onp.float32)]
        # meshio_mesh.cell_data['debug'] = [onp.array(dmass_output, dtype=onp.float32)]
        meshio_mesh.write(os.path.join(vtk_dir, f'sol_{step:04d}.vtu'))


    clean_sols(data_dir)

    Nx, Ny, Nz = lbm_args['Nx']['value'], lbm_args['Ny']['value'], lbm_args['Nz']['value']
    domain_x, domain_y, domain_z = Nx, Ny, Nz
    cell_centroids = compute_cell_centroid(meshio_mesh)
    ts, xs, ys, ps = read_path()
    total_time_steps = len(ts) - 1

    vels = np.array([[0, 1, -1, 0,  0, 0,  0, 1, -1 , 1, -1, 1, -1, -1,  1, 0,  0,  0,  0],
                     [0, 0,  0, 1, -1, 0,  0, 1, -1, -1,  1, 0,  0,  0,  0, 1, -1,  1, -1],
                     [0, 0,  0, 0,  0, 1, -1, 0,  0,  0,  0, 1, -1,  1, -1, 1, -1, -1,  1]])
    rev = np.array([0, 2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15, 18, 17])
    weights = np.array([1./3., 1./18., 1./18., 1./18., 1./18., 1./18., 1./18., 1./36., 1./36., 1./36.,
                        1./36., 1./36., 1./36., 1./36., 1./36., 1./36., 1./36., 1./36., 1./36.])

    m = 0.5
    Ns = 19
    cs_sq = 1./3.
    theta = 1e-3

    h = 1.
    dt = 1.
    rho0 = 1.
    T0 = 1.
    M0 = 1.
    
    p_atm_real = 101325 # [Pa]
    gas_const_real = 8.314 # [J/(K*mol)]
    SB_const_real = 5.67e-8 # [kg*s^-3*K^-4]

    C_length = lbm_args['h']['value']/h
    C_time = lbm_args['dt']['value']/dt
    C_density = lbm_args['rho0']['value']/rho0
    C_temperature = lbm_args['T0']['value']/T0
    C_molar_mass = lbm_args['M0']['value']/M0
    C_mass = C_density*C_length**3
    C_force = C_mass*C_length/(C_time**2)
    C_energy = C_force*C_length
    C_pressure = C_force/C_length**2
    C_molar = C_mass/C_molar_mass

    p_atm = p_atm_real/C_pressure
    gas_const = gas_const_real/(C_energy/(C_temperature*C_molar))
    SB_const = SB_const_real/(C_mass/C_time**3/C_temperature**4)

    gravity = lbm_args['gravity']['value']/(C_length/C_time**2)
    viscosity_mu = lbm_args['dynamic_viscosity']['value']/(C_mass/(C_length*C_time))
    st_coeff = lbm_args['st_coeff']['value']/(C_force/C_length)
    st_grad_coeff = lbm_args['st_grad_coeff']['value']/(C_force/(C_length*C_temperature))
    rp_coeff = lbm_args['rp_coeff']['value']
    laser_power = lbm_args['laser_power']['value']/(C_energy/C_time)
    beam_size = lbm_args['beam_size']['value']/C_length
    absorbed_fraction = lbm_args['absorbed_fraction']['value']
    scanning_vel = lbm_args['scanning_vel']['value']/(C_length/C_time)
    heat_capacity = lbm_args['heat_capacity']['value']/(C_energy/(C_mass*C_temperature))
    thermal_diffusivitity_l = lbm_args['thermal_diffusivitity_l']['value']/(C_length**2/C_time)
    thermal_diffusivitity_s = lbm_args['thermal_diffusivitity_s']['value']/(C_length**2/C_time)
    emissivity = lbm_args['emissivity']['value']
    h_conv = lbm_args['h_conv']['value']/(C_mass/C_time**3/C_temperature)
    latent_heat_fusion = lbm_args['latent_heat_fusion']['value']/(C_energy/C_mass)
    latent_heat_evap = lbm_args['latent_heat_evap']['value']/(C_energy/C_mass)
    T_liquidus = lbm_args['T_liquidus']['value']/C_temperature
    T_solidus = lbm_args['T_solidus']['value']/C_temperature
    T_evap = lbm_args['T_evap']['value']/C_temperature
    enthalpy_s = lbm_args['enthalpy_s']['value']/(C_energy/C_mass)
    enthalpy_l = lbm_args['enthalpy_l']['value']/(C_energy/C_mass)

    viscosity_nu = viscosity_mu/rho0
    tau_viscosity_nu = viscosity_nu/(cs_sq*dt) + 0.5
    tau_diffusivity_l = thermal_diffusivitity_l/(cs_sq*dt) + 0.5
    tau_diffusivity_s = thermal_diffusivitity_s/(cs_sq*dt) + 0.5
    rho_g = rho0
    g = np.array([0., 0., -gravity])

    # assert tau_viscosity_nu < 1., f"Warning: tau_viscosity_nu = {tau_viscosity_nu} is out of range [0.5, 1] - may cause numerical instability"
    print(f"Relaxation parameter tau_viscosity_nu = {tau_viscosity_nu}, tau_diffusivity_s = {tau_diffusivity_s}, surface tensiont coeff = {st_coeff}")
    print(f"Lattice = ({Nx}, {Ny}, {Nz}), size = {lbm_args['h']['value']*1e6} micro m")

    phase = initial_phase #(N_all)
    f_distribute = np.tile(weights, (Nx, Ny, Nz, 1)) * rho0   #(Nx,Ny,Nz,19)
    h_distribute = np.tile(weights, (Nx, Ny, Nz, 1)) * T0*heat_capacity #(Nx,Ny,Nz,19)
    mass = np.sum(f_distribute, axis=-1)   # (Nx,Ny,Nz)
    rho = compute_rho(f_distribute) # (Nx,Ny,Nz)
    u = np.zeros((Nx, Ny, Nz, 3))
    enthalpy = compute_enthalpy(h_distribute) # (Nx,Ny,Nz)
    T = compute_T(enthalpy) # (Nx,Ny,Nz)
    lattice_ids = np.arange(Nx*Ny*Nz) # (N_all)
    f_distribute, h_distribute, phase, mass = reini_gas_to_lg_vmap(lattice_ids, f_distribute, h_distribute, rho, u, enthalpy, T, phase, mass)
    mass = np.where(phase == ST.LG, 0.5*np.sum(f_distribute, axis=-1), mass)

    total_mass = np.sum(compute_total_mass_vmap(lattice_ids, f_distribute, phase, mass))

    melted = np.zeros_like(mass)
    output_result(data_dir, meshio_mesh, f_distribute, h_distribute, phase, mass, np.zeros_like(mass), melted, 0)

    start_time = time.time()
    for i in range(total_time_steps):
        # print(f"Step {i}")
        # print(f"Initial mass = {np.sum(compute_total_mass_vmap(lattice_ids, f_distribute, phase, mass))}")
        # crt_t = (i + 1)*dt

        if fluid_only:
            h_distribute = np.tile(weights, (Nx, Ny, Nz, 1)) * (enthalpy_l + 1.)

        rho = compute_rho(f_distribute)
        enthalpy = compute_enthalpy(h_distribute)
        T = compute_T(enthalpy)
        vof = compute_vof(rho, phase, mass)
        phi, kappa = compute_curvature(lattice_ids, vof)
        T_grad = compute_T_grad(lattice_ids, T, phase)

        f_source_term = compute_f_source_term(rho, vof, phi, kappa, T, T_grad)
        h_source_term = compute_h_source_term(lattice_ids, T, vof, phi, cell_centroids, xs[i + 1]/C_length, ys[i + 1]/C_length, ps[i + 1])
        u = compute_u(f_distribute, rho, T, f_source_term)

        f_distribute = collide_f_vmap(lattice_ids, f_distribute, rho, T, u, phase, f_source_term)
        h_distribute = collide_h_vmap(lattice_ids, h_distribute, enthalpy, T, rho, u, phase, h_source_term)

        f_distribute, mass = update_f_vmap(lattice_ids, f_distribute, rho, u, phase, mass, vof)
        h_distribute = update_h_vmap(lattice_ids, h_distribute, u, phase)

        # print(f"max kappa = {np.max(kappa)}, min kappa = {np.min(kappa)}")
        # print(f"After update, mass = {np.sum(compute_total_mass_vmap(lattice_ids, f_distribute, phase, mass))}")
        
        phase = reini_lg_to_liquid_vmap(lattice_ids, f_distribute, phase, mass)
        # print(f"After lg_to_liquid, total mass = {np.sum(compute_total_mass_vmap(lattice_ids, f_distribute, phase, mass))}")

        # rho, u = compute_rho_u(f_distribute)
        # rho = compute_rho(f_distribute)
        # u = compute_u(f_distribute, rho, f_source_term)
        f_distribute, h_distribute, phase, mass = reini_gas_to_lg_vmap(lattice_ids, f_distribute, h_distribute, rho, u, enthalpy, T, phase, mass)
        # print(f"After gas_to_lg, total mass = {np.sum(compute_total_mass_vmap(lattice_ids, f_distribute, phase, mass))}")

        phase = reini_lg_to_gas_vmap(lattice_ids, f_distribute, phase, mass)
        # print(f"After lg_to_gas, mass = {np.sum(compute_total_mass_vmap(lattice_ids, f_distribute, phase, mass))}")

        phase, mass = reini_liquid_to_lg_vmap(lattice_ids, f_distribute, phase, mass)
        # print(f"After liquid_to_lg, total mass = {np.sum(compute_total_mass_vmap(lattice_ids, f_distribute, phase, mass))}")

        phase = adhoc_step_vmap(lattice_ids, f_distribute, phase, mass)

        calculated_mass = np.sum(compute_total_mass_vmap(lattice_ids, f_distribute, phase, mass))
        mass = np.where(phase == ST.LG, mass + (total_mass - calculated_mass)/np.sum(phase == ST.LG), mass)

        f_distribute, h_distribute, phase, mass = refresh_for_output_vmap(lattice_ids, f_distribute, h_distribute, phase, mass)
        # print(f"After refresh, total mass = {np.sum(compute_total_mass_vmap(lattice_ids, f_distribute, phase, mass))}")
 
        melted = np.where(T > T_solidus, 1., melted)
        inverval = lbm_args['output_interval']
        if (i + 1) % inverval == 0:
            print(f"Step {i + 1} in {total_time_steps}")
            output_result(data_dir, meshio_mesh, f_distribute, h_distribute, phase, mass, kappa, melted, (i + 1) // inverval)

    end_time = time.time()
    print(f"Total wall time = {end_time - start_time}")
